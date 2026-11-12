# -*- coding: utf-8 -*-
"""Isolated tests for console_model_actions (Revision 5 L3/L1).

All model_sync state files and client configs (DSH settings.yaml / ZCode
config.json) are redirected into a temporary directory by mock.patching the
module constants (model_sync's injectable surface is exactly those module
constants). No real ~/.dsh, ~/.zcode, gateway data directory, catalog HTTP
endpoint or Windows scheduled task is ever touched. The scheduled-task
boundary is faked at ``subprocess.run`` so the real argument-list build,
output decode and exit-code handling in ``_run_task_script`` stay under test;
the fake child simulates ``Register-ScheduledTask -Force`` idempotency with
one registration slot.

The preview zero-write assertion covers the real write targets (DSH/ZCode
client configs, state, last-result, history). ``model_sync``'s existing
preview semantics persist its own preview artifact inside its own DATA_DIR;
that artifact is asserted to round-trip the returned preview verbatim instead
of being counted as a client write.
"""

import contextlib
import json
import re
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402  (same requirement as model_sync itself)

from console import adapters as console_adapters  # noqa: E402
from console import model_actions as console_model_actions  # noqa: E402
from model_sync import config_adapters as model_config_adapters  # noqa: E402
from model_sync import sync as model_sync  # noqa: E402
from model_sync import sync_core as model_sync_core  # noqa: E402
from model_sync.sync_core import CatalogModel  # noqa: E402
from console.operations import OperationCancelled, OperationError  # noqa: E402

DSH_VALID = """\
llm-pi-ai:
  providers:
    openwebui:
      displayName: UM
      models:
    m365:
      displayName: M365
      models:
"""

ZCODE_VALID = """{
  "provider": {
    "local:openwebui": {"name": "UM", "models": {}},
    "local:m365": {"name": "M365", "models": {}},
    "local:lingma-proxy": {"name": "Qoder（国内）", "models": {}}
  }
}
"""

PIN_KEY = "um|DSH|openwebui|model-a"


def _stub_catalogs(source="um", model_id="model-a", context=32768):
    return {source: [CatalogModel(source, model_id, context=context)]}


def _aligned_store(catalogs):
    """规则库正常且与目录对齐——R20-2「写主配置」路径的前提。

    规则库不可用（缺席／不可读／无法解析）时整个 ZCode 写集合会延期，所以
    「写主配置」类用例必须给出一个正常的规则库；这里按目录构造每个受管
    ZCode provider 的规则块（白名单/排序取目录发布面、模型规则带 context），
    渲染结果与原文档相等，本轮不产生规则库变化。
    """
    order: list = []
    provider_rules: list = []
    model_rules: list = []
    for binding in model_sync.ACTIVE_BINDINGS:
        catalog = catalogs.get(binding.source)
        if binding.consumer != "ZCode" or not catalog:
            continue
        order.append(binding.provider)
        provider_rules.append({
            "providerId": binding.provider,
            "providerName": binding.provider_name,
            "enabled": True,
            "config": {
                "group": "standard-personal",
                "personalModelIds": [m.model_id for m in catalog],
                "modelOrder": [m.model_id for m in catalog],
            },
        })
        for model in catalog:
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


@contextlib.contextmanager
def temp_sync_env(dsh_text=DSH_VALID, zcode_text=ZCODE_VALID,
                  state_text=None, history_text=None):
    """Point every model_sync path constant at an isolated temp layout."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        data = root / "data"
        dsh = root / "home" / ".dsh" / "settings.yaml"
        zcode = root / "home" / ".zcode" / "v2" / "config.json"
        dsh.parent.mkdir(parents=True, exist_ok=True)
        zcode.parent.mkdir(parents=True, exist_ok=True)
        data.mkdir(parents=True, exist_ok=True)
        dsh.write_text(dsh_text, encoding="utf-8")
        zcode.write_text(zcode_text, encoding="utf-8")
        paths = {
            "DATA_DIR": data,
            "STATE_FILE": data / "model-sync-state.json",
            "RESULT_FILE": data / "model-sync-last-result.json",
            "PREVIEW_FILE": data / "model-sync-last-preview.json",
            "HISTORY_FILE": data / "model-sync-history.jsonl",
            "BACKUP_DIR": data / "model-sync-backups",
            "LOCK_FILE": data / "model-sync.lock",
            "DSH_YAML": dsh,
            "ZCODE_JSON": zcode,
            # The plan path also reads the ZCode provider rule store, and the
            # apply path rewrites it when the plan changes it: without this entry
            # the fixture would read the operator's real
            # ~/.zcode/v2/provider_config.json (polluting every plan with its
            # live rule-library orphans) and an apply would rewrite that
            # production client config.
            "ZCODE_PROVIDER_CONFIG": zcode.parent / "provider_config.json",
        }
        if state_text is not None:
            paths["STATE_FILE"].write_text(state_text, encoding="utf-8")
        if history_text is not None:
            paths["HISTORY_FILE"].write_text(history_text, encoding="utf-8")
        with contextlib.ExitStack() as stack:
            for name, value in paths.items():
                stack.enter_context(mock.patch.object(model_sync, name, value))
            yield paths


# Real write targets of the sync chain; snapshots assert byte+mtime identity.
_SNAPSHOT_TARGETS = ("STATE_FILE", "RESULT_FILE", "HISTORY_FILE",
                     "DSH_YAML", "ZCODE_JSON")


def _snapshot(paths, names=_SNAPSHOT_TARGETS):
    snap = {}
    for name in names:
        path = paths[name]
        snap[name] = (path.read_bytes(), path.stat().st_mtime_ns) \
            if path.exists() else None
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


class _FakeTaskProcess:
    """In-memory stand-in for the powershell child process.

    Patched at ``subprocess.run`` so the real ``_run_task_script`` stays in
    the tested path (argument-list build, decode, exit-code handling). The
    fake reproduces the script's observable idempotency contract: Repair
    uses ``Register-ScheduledTask -Force`` (re-registration overwrites one
    task slot; no duplicate is ever created) and Status is a pure read.
    """

    def __init__(self):
        self.commands = []
        self.task = None
        self.registrations = 0
        self.disables = 0
        self.fail = False

    def __call__(self, cmd, capture_output=True, timeout=None):
        self.commands.append(list(cmd))
        action = cmd[-1]
        if action == "Repair":
            self.registrations += 1
            self.task = {"name": "SelfUseAI AI Gateway Model Sync"}
        if action == "Disable":
            # The script's real write (Disable-ScheduledTask); tracked so
            # the adapter-level tests can assert the task was "disabled"
            # only inside the fake, never on the real system.
            self.disables += 1
            self.task = None
        if self.fail:
            return types.SimpleNamespace(
                returncode=1, stdout=b"", stderr=b"boom: register failed\n")
        state = "未安装" if self.task is None else "Ready"
        return types.SimpleNamespace(
            returncode=0,
            stdout=("状态：%s\n下次运行：2026-09-16 06:00\n" % state).encode("utf-8"),
            stderr=b"")

    def assert_command(self, test, cmd, action):
        """The adapter must call the fixed script via an argument list."""
        test.assertEqual(action, cmd[-1])
        test.assertEqual("-Action", cmd[-2])
        test.assertEqual(str(console_model_actions.TASK_SCRIPT),
                         cmd[cmd.index("-File") + 1])
        test.assertNotIn("cmd.exe", cmd[0])


class ModelSyncPreviewTests(unittest.TestCase):
    def test_preview_returns_verbatim_result_and_writes_nothing(self):
        with temp_sync_env() as paths:
            before = _snapshot(paths)
            # zcode_running=True is the old-console "pending" fixture flavour;
            # it only manifests on the apply path, preview stays zero-write.
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(_stub_catalogs(), [])), \
                    mock.patch.object(model_sync, "zcode_running",
                                      return_value=True):
                result = console_model_actions.model_sync_preview(
                    "", {"scope_mode": "all"}, _Ctx())
            _assert_unchanged(self, paths, before)
            preview = result["preview"]
            self.assertEqual(preview["status"], "preview")
            self.assertEqual(preview["applied"], [])
            # The stub catalog publishes model-a against empty consumers, so
            # the recomputed plan is the only real signal here (applied and
            # pending are both empty by construction in preview mode).
            self.assertTrue(preview["plan"], "preview must carry the plan")
            self.assertTrue(any("发布 model-a" in line for line in preview["plan"]))
            self.assertIn("未写入", result["note"])
            # model_sync's existing preview artifact round-trips verbatim.
            persisted = json.loads(paths["PREVIEW_FILE"].read_text("utf-8"))
            self.assertEqual(preview, persisted)
            self.assertFalse(paths["RESULT_FILE"].exists())
            self.assertFalse(paths["HISTORY_FILE"].exists())
            self.assertFalse(paths["STATE_FILE"].exists())

    def test_preview_scope_params_reach_run_sync(self):
        with temp_sync_env() as paths:
            before = _snapshot(paths)
            with mock.patch.object(
                    model_sync, "run_sync",
                    return_value={"status": "preview"}) as run:
                console_model_actions.model_sync_preview(
                    "", {"scope_mode": "selected", "sources": ["um"]}, _Ctx())
            scope = run.call_args[1]["scope"]
            self.assertEqual(scope["sources"], ["um"])
            self.assertIsNone(scope["consumers"])
            self.assertEqual(scope["models"], {})
            _assert_unchanged(self, paths, before)
        with self.assertRaises(OperationError) as caught:
            console_model_actions.model_sync_preview(
                "", {"scope_mode": "selected", "sources": ["bogus"]}, _Ctx())
        self.assertEqual("invalid_params", caught.exception.code)

    def test_preview_failure_maps_to_sync_failed(self):
        with temp_sync_env() as paths:
            before = _snapshot(paths)
            with mock.patch.object(model_sync, "run_sync",
                                   side_effect=RuntimeError("boom\nline2")):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_sync_preview(
                        "", {"scope_mode": "all"}, _Ctx())
            self.assertEqual("sync_failed", caught.exception.code)
            self.assertNotIn("\n", caught.exception.message)
            _assert_unchanged(self, paths, before)


class ModelSyncApplyTests(unittest.TestCase):
    def test_combined_selection_is_validated_fail_closed(self):
        # §11.3 组合窗口：selection 缺席 = 既有普通同步语义（不消费草稿）；
        # 空对象/非法版本/非 source|model_id 键一律 invalid_params，绝不
        # 静默退回普通同步（否则草稿永远不发布或误消费）。
        catalogs = _stub_catalogs()
        with temp_sync_env() as paths:
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(_aligned_store(catalogs), ensure_ascii=False),
                encoding="utf-8")
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(catalogs, [])), \
                    mock.patch.object(model_sync, "zcode_running",
                                      return_value=False), \
                    mock.patch.object(model_sync, "run_sync",
                                      return_value={"status": "normal"}) as run:
                for bad in ({}, {"um|model-a": 0}, {"um|model-a": True},
                            {"um|model-a": "1"}, {"no-separator": 1},
                            {"|": 1}, {"um|": 1}):
                    with self.subTest(selection=bad):
                        with self.assertRaises(OperationError) as caught:
                            console_model_actions.model_sync_apply(
                                "", {"scope_mode": "all",
                                      "selection": bad}, _Ctx())
                        self.assertEqual("invalid_params",
                                         caught.exception.code)
                run.assert_not_called()
                # A malformed ordinary-preview token is rejected before run_sync.
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_sync_apply(
                        "", {"scope_mode": "all",
                              "preview_token": "invalid"}, _Ctx())
                self.assertEqual("invalid_params", caught.exception.code)
                run.assert_not_called()
                # 组合发布必须带回组合预览的 token。
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_sync_apply(
                        "", {"scope_mode": "all",
                              "selection": {"um|model-a": 1}}, _Ctx())
                self.assertEqual("invalid_params", caught.exception.code)
                run.assert_not_called()

    def test_selection_outside_the_scope_is_rejected(self):
        # §11.3.2：范围外草稿保留——selection 绝不借组合窗口扩大写入面。
        catalogs = _stub_catalogs()
        with temp_sync_env() as paths:
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(_aligned_store(catalogs), ensure_ascii=False),
                encoding="utf-8")
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(catalogs, [])), \
                    mock.patch.object(model_sync, "zcode_running",
                                      return_value=False), \
                    mock.patch.object(model_sync, "run_sync",
                                      return_value={"status": "normal"}) as run:
                for scope in (
                        {"scope_mode": "selected", "sources": '["m365"]',
                         "selection": {"um|model-a": 1}},
                        {"scope_mode": "selected", "sources": '["um"]',
                         "models": {"um": ["other-model"]},
                         "selection": {"um|model-a": 1}}):
                    with self.subTest(scope=scope):
                        with self.assertRaises(OperationError) as caught:
                            console_model_actions.model_sync_apply("", scope, _Ctx())
                        self.assertEqual("invalid_params",
                                         caught.exception.code)
                run.assert_not_called()

    def test_combined_preview_returns_the_bound_token_and_selection(self):
        # §11.3：组合预览走同一 run_sync（只读），带回 selection 与
        # preview_token；省略 selection 保持既有普通预览语义。
        catalogs = _stub_catalogs()
        state = model_sync.empty_state()
        state["modelSettings"] = {"um|model-a": {
            "draft": {"alias": "renamed", "contextSource": None,
                      "contextTokens": None, "defaultEffort": None},
            "draftVersion": 1, "draftSavedAt": "t",
            "applied": None, "appliedVersion": None,
            "appliedAt": None, "appliedEvidence": None}}
        with temp_sync_env(state_text=json.dumps(state, ensure_ascii=False)) as paths:
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(_aligned_store(catalogs), ensure_ascii=False),
                encoding="utf-8")
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(catalogs, [])), \
                    mock.patch.object(model_sync, "zcode_running",
                                      return_value=False):
                result = console_model_actions.model_sync_preview(
                    "", {"scope_mode": "all",
                          "selection": {"um|model-a": 1}}, _Ctx())
                self.assertEqual({"um|model-a": 1}, result["selection"])
                self.assertEqual(64, len(result["preview_token"]))
                plain = console_model_actions.model_sync_preview(
                    "", {"scope_mode": "all"}, _Ctx())
                self.assertIsNone(plain["selection"])
                self.assertRegex(plain["preview_token"], r"^[0-9a-f]{64}$")

    def test_fixture_isolates_the_provider_rule_store(self):
        # Guard for the fixture contract: the plan path reads the ZCode provider
        # rule store. If it fell through to the real path, every plan here would
        # inherit the operator's live rule-library orphans and an apply could
        # rewrite a production client config.
        real = Path.home() / ".zcode" / "v2" / "provider_config.json"
        with temp_sync_env() as paths:
            self.assertNotEqual(real, model_sync.ZCODE_PROVIDER_CONFIG)
            self.assertEqual(paths["ZCODE_PROVIDER_CONFIG"],
                             model_sync.ZCODE_PROVIDER_CONFIG)
            self.assertEqual(paths["ZCODE_PROVIDER_CONFIG"].parents[1],
                             paths["ZCODE_JSON"].parents[1])

    def test_apply_succeeds_with_backup_and_parsed_output(self):
        with temp_sync_env() as paths:
            catalogs = _stub_catalogs()
            # R20-2：规则库正常且与目录对齐（「写主配置」路径的前提）。
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(_aligned_store(catalogs), ensure_ascii=False),
                encoding="utf-8")
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(catalogs, [])), \
                    mock.patch.object(model_sync, "zcode_running",
                                      return_value=False), \
                    mock.patch.object(model_sync, "_get_json",
                                      side_effect=OSError("offline")):
                result = console_model_actions.model_sync_apply(
                    "", {"scope_mode": "all"}, _Ctx())
            # Rev11 单文件发布：规则库与目录对齐（no-op），config.json 不是
            # 第二个实时权威——ZCode 侧本轮零写入，只有 DSH 侧发布。
            self.assertEqual(1, len(result["applied"]))
            self.assertEqual("changed", result["status"])
            self.assertIn("已应用 1 项", result["summary"])
            # Structured mirrors of the applied changes ride along additively.
            self.assertEqual(1, len(result["appliedDetails"]))
            self.assertEqual({"DSH"},
                             {d["consumer"] for d in result["appliedDetails"]})
            self.assertEqual(
                {d["description"] for d in result["appliedDetails"]},
                set(result["applied"]))
            self.assertEqual([], result["pendingDetails"])
            # Backups exist only for the replaced client config (one stamp dir):
            # the state file is new and the rule store was not rewritten.
            stamp_dirs = [p for p in paths["BACKUP_DIR"].iterdir()
                          if p.is_dir()]
            self.assertEqual(1, len(stamp_dirs))
            backups = sorted(p.name for p in stamp_dirs[0].iterdir())
            self.assertEqual(["settings.yaml.bak"], backups)
            # Final files pass their own parsers (解析校验过).
            dsh = yaml.safe_load(paths["DSH_YAML"].read_text(encoding="utf-8"))
            models = dsh["llm-pi-ai"]["providers"]["openwebui"]["models"]
            self.assertEqual("model-a（UM）", models[0]["name"])
            zcode = json.loads(
                paths["ZCODE_JSON"].read_text(encoding="utf-8"))
            self.assertEqual(zcode["provider"]["local:openwebui"]["models"], {})
            store = json.loads(
                paths["ZCODE_PROVIDER_CONFIG"].read_text(encoding="utf-8"))
            rule = next(
                rule for rule in store["config"]["providerConfigRules"]
                ["providerRules"] if rule["providerId"] == "local:openwebui")
            self.assertEqual(rule["config"]["personalModelIds"], ["model-a"])
            last = json.loads(
                paths["RESULT_FILE"].read_text(encoding="utf-8"))
            self.assertEqual("changed", last["status"])
            self.assertTrue(paths["HISTORY_FILE"].exists())

    def test_apply_rejects_retired_source_before_any_write(self):
        """B0：退役来源（CLIProxy）不在默认装配内：显式选择被明确拒绝，
        不读它的旧凭据文件、不创建 provider、不写任何客户端配置。"""
        key = "sentinel-cliproxy-gateway-key-3a9"
        with temp_sync_env() as paths:
            key_file = paths["DATA_DIR"] / "gateway-api-key.txt"
            key_file.write_text(key + "\n", encoding="utf-8")
            before = _snapshot(paths)
            catalogs = {"cliproxy": [CatalogModel("cliproxy", "gemini-3-flash")]}
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(catalogs, [])), \
                    mock.patch.object(model_sync, "zcode_running",
                                      return_value=False):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_sync_apply(
                        "", {"scope_mode": "selected",
                             "sources": ["cliproxy"]},
                        _Ctx())
            self.assertEqual("invalid_params", caught.exception.code)
            self.assertIn("已暂停", caught.exception.message)
            _assert_unchanged(self, paths, before)
            self.assertFalse(paths["ZCODE_PROVIDER_CONFIG"].exists())

    def test_apply_selected_empty_selection_rejected(self):
        # 空选拒绝，不回退全量。
        for raw in ({"scope_mode": "selected"},
                    {"scope_mode": "selected", "sources": []},
                    {"scope_mode": "selected", "consumers": []},
                    {"scope_mode": "selected", "models": {}}):
            with self.subTest(params=raw):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_sync_apply("", raw, _Ctx())
                self.assertEqual("invalid_params", caught.exception.code)
        with self.assertRaises(OperationError) as caught:
            console_model_actions.model_sync_apply(
                "", {"scope_mode": "everything"}, _Ctx())
        self.assertEqual("invalid_params", caught.exception.code)

    def test_apply_with_zcode_running_stays_pending(self):
        with temp_sync_env() as paths:
            before = _snapshot(paths, ("DSH_YAML", "ZCODE_JSON"))
            catalogs = _stub_catalogs("um", "um-model", 4096)
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(catalogs, [])), \
                    mock.patch.object(model_sync, "zcode_running",
                                      return_value=True), \
                    mock.patch.object(model_sync, "zcode_hot_sync_admission",
                                      return_value=(False, "测试：未准入")), \
                    mock.patch.object(model_sync, "_get_json",
                                      side_effect=OSError("offline")):
                result = console_model_actions.model_sync_apply(
                    "", {"scope_mode": "selected",
                         "consumers": ["ZCode"]}, _Ctx())
            self.assertEqual([], result["applied"])
            self.assertEqual("pending", result["status"])
            self.assertIn("等待应用", result["summary"])
            # 结构化镜像：第一条是延期变更，随后是延期原因条目（R20-2 起
            # pendingDetails 与 pending 口径一致，原因不再只存在于平面列表）。
            details = result["pendingDetails"]
            self.assertEqual(
                {"consumer": "ZCode", "kind": "add", "model_id": "um-model",
                 "description": details[0]["description"]},
                details[0])
            self.assertTrue(any(d["kind"] == "provider_write_deferred"
                                and "整体延期" in d["description"]
                                for d in details))
            self.assertTrue(any("整体延期" in line
                                for line in result["pending"]))
            # Deferred changes: no client config was rewritten.
            _assert_unchanged(self, paths, before)
            self.assertTrue(paths["RESULT_FILE"].exists())

    def test_apply_scope_params_reach_run_sync(self):
        with temp_sync_env() as paths:
            with mock.patch.object(model_sync, "run_sync",
                                   return_value={"status": "normal"}) as run:
                console_model_actions.model_sync_apply(
                    "", {"scope_mode": "selected",
                         "sources": ["um"],
                         "consumers": '["ZCode"]',
                         "models": {"um": ["model-a"]}}, _Ctx())
            scope = run.call_args[1]["scope"]
            self.assertEqual(scope["sources"], ["um"])
            self.assertEqual(scope["consumers"], ["ZCode"])
            self.assertEqual(scope["models"], {"um": ["model-a"]})

    def test_apply_scope_params_are_validated(self):
        cases = [
            {"sources": ["bogus"]},
            {"sources": "not json"},
            {"sources": {"um": True}},
            {"consumers": ["Slack"]},
            {"consumers": []},
            {"models": {"bogus": ["m"]}},
            {"models": {"um": [""]}},
            {"models": {"um": "m"}},
            {"models": {("um",): ["m"]}},
        ]
        for raw in cases:
            params = dict(raw, scope_mode="selected")
            with self.subTest(params=params):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_sync_apply("", params, _Ctx())
                self.assertEqual("invalid_params", caught.exception.code)

    def test_apply_models_entry_caps_are_enforced(self):
        too_many = {"scope_mode": "selected",
                    "models": {"um": ["m%d" % i for i in range(65)]}}
        with self.assertRaises(OperationError) as caught:
            console_model_actions.model_sync_apply("", too_many, _Ctx())
        self.assertEqual("invalid_params", caught.exception.code)
        oversized = {"scope_mode": "selected", "models": {
            "um": ["m%d" % i for i in range(64)],
            "m365": ["n%d" % i for i in range(64)],
            "qoder_cn": ["p%d" % i for i in range(64)],
            "qoder_global": ["q%d" % i for i in range(64)],
            "um ": ["x%d" % i for i in range(1)],
        }}
        with self.assertRaises(OperationError) as caught:
            console_model_actions.model_sync_apply("", oversized, _Ctx())
        self.assertEqual("invalid_params", caught.exception.code)

    def test_apply_with_bad_config_fails_and_leaves_targets_intact(self):
        with temp_sync_env(dsh_text="llm-pi-ai: [broken") as paths:
            before = _snapshot(paths)
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(_stub_catalogs(), [])):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_sync_apply(
                        "", {"scope_mode": "all"}, _Ctx())
            self.assertEqual("sync_failed", caught.exception.code)
            self.assertIn("模型同步失败", caught.exception.message)
            self.assertNotIn("\n", caught.exception.message)
            _assert_unchanged(self, paths, before)
            self.assertFalse(paths["BACKUP_DIR"].exists())

    def test_apply_rejects_the_removed_probe_param(self):
        # Rev8 契约 2：组合动作「应用并探测」已移除——任何 probe 传入都明确
        # 拒绝并说明去向（独立 model.context_probe_run），绝不静默探测。
        for probe in ("yes", True, False):
            with self.subTest(probe=probe):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_sync_apply(
                        "", {"scope_mode": "all", "probe": probe}, _Ctx())
                self.assertEqual("invalid_params", caught.exception.code)
                self.assertIn("context_probe_run", caught.exception.message)

    def test_sync_lock_busy_maps_to_sync_failed(self):
        with temp_sync_env():
            with mock.patch.object(model_sync, "SyncLock",
                                   side_effect=RuntimeError(
                                       "另一个模型同步正在运行")):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_sync_apply(
                        "", {"scope_mode": "all"}, _Ctx())
            self.assertEqual("sync_failed", caught.exception.code)
            self.assertIn("另一个模型同步正在运行", caught.exception.message)
            self.assertNotIn("\n", caught.exception.message)

    def test_native_exception_maps_to_single_line_chinese(self):
        with temp_sync_env():
            with mock.patch.object(model_sync, "run_sync",
                                   side_effect=RuntimeError("boom\nsecond")):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_sync_apply(
                        "", {"scope_mode": "all"}, _Ctx())
            self.assertEqual("sync_failed", caught.exception.code)
            message = caught.exception.message
            self.assertTrue(message.startswith("模型同步失败："))
            self.assertNotIn("\n", message)
            self.assertIn("RuntimeError", message)
            self.assertIn("boom second", message)


class ModelSyncPendingHistoryTests(unittest.TestCase):
    def test_pending_reads_state_and_last_result(self):
        state = json.dumps({
            "version": 2, "pending": ["ZCode 发布 model-a"], "pinned": []},
            ensure_ascii=False)
        last = json.dumps({"status": "pending", "pending": ["x"]})
        with temp_sync_env(state_text=state) as paths:
            paths["RESULT_FILE"].write_text(last, encoding="utf-8")
            before = _snapshot(paths)
            result = console_model_actions.model_sync_pending("", {}, _Ctx())
            _assert_unchanged(self, paths, before)
        self.assertEqual(["ZCode 发布 model-a"], result["pending"])
        self.assertEqual({"status": "pending", "pending": ["x"]},
                         result["last_result"])
        self.assertIsNone(result["last_result_error"])

    def test_pending_without_files_is_legal_empty(self):
        with temp_sync_env():
            result = console_model_actions.model_sync_pending("", {}, _Ctx())
        self.assertEqual([], result["pending"])
        self.assertIsNone(result["last_result"])
        self.assertIsNone(result["last_result_error"])

    def test_pending_exposes_active_state_without_erasing_paused_history(self):
        state = json.dumps({
            "version": 2, "pending": ["ZCode 发布 model-b"],
            "pinned": [PIN_KEY, "m365|DSH|m365|historical"],
            "probes": {
                "um|DSH|openwebui|model-a": {"status": "ok"},
                "m365|historical": {"status": "pending"}},
            "catalogCounts": {"um": 3, "m365": 9},
            "lastStatus": "changed", "lastRun": "2026-09-16T06:00:00",
        }, ensure_ascii=False)
        with temp_sync_env(state_text=state) as paths:
            before = _snapshot(paths)
            result = console_model_actions.model_sync_pending("", {}, _Ctx())
            _assert_unchanged(self, paths, before)
            migrated = model_sync.load_state()
        # The endpoint filters its view; stored history remains unchanged.
        self.assertEqual(
            {"um|DSH|openwebui|model-a": {"status": "ok"}},
            result["probes"])
        # Rev8 B8-2: the pin mechanism is removed — the pending read no
        # longer exposes a pinned list (the v2 state doc is migrated to v3
        # in memory; the file itself stays untouched on a read-only path).
        self.assertNotIn("pinned", result)
        self.assertEqual(model_sync_core.STATE_VERSION, migrated["version"])
        self.assertNotIn("pinned", migrated)
        self.assertEqual({"um": 3}, result["catalog_counts"])
        self.assertEqual(["cliproxy", "m365", "qoder_cn", "qoder_global"],
                         result["paused_sources"])
        self.assertEqual("changed", result["last_status"])
        self.assertEqual("2026-09-16T06:00:00", result["last_run"])

    def test_pending_additional_fields_default_when_absent(self):
        # Absent state file: load_state falls back to empty_state(), whose
        # probes/catalogCounts defaults pass through verbatim ({} / {}).
        with temp_sync_env():
            result = console_model_actions.model_sync_pending("", {}, _Ctx())
        self.assertEqual({}, result["probes"])
        self.assertNotIn("pinned", result)
        self.assertEqual({}, result["catalog_counts"])
        self.assertIsNone(result["last_status"])
        self.assertIsNone(result["last_run"])
        # A v2 state file without those keys forwards None verbatim
        # (pure state.get pass-through, no fabricated defaults here).
        legacy = json.dumps({"version": 2, "pending": []})
        with temp_sync_env(state_text=legacy):
            result = console_model_actions.model_sync_pending("", {}, _Ctx())
        self.assertIsNone(result["probes"])
        self.assertIsNone(result["last_status"])

    def test_pending_reports_corrupt_result_file(self):
        with temp_sync_env() as paths:
            paths["RESULT_FILE"].write_text("{not json", encoding="utf-8")
            result = console_model_actions.model_sync_pending("", {}, _Ctx())
        self.assertIsNone(result["last_result"])
        self.assertTrue(result["last_result_error"])

    def test_history_is_bounded_and_skips_corrupt_lines(self):
        lines = [json.dumps({"timestamp": "2026-01-%02d" % i, "n": i})
                 for i in range(1, 26)]
        lines.append("{corrupt")
        history_text = "\n".join(lines) + "\n"
        with temp_sync_env(history_text=history_text) as paths:
            before = _snapshot(paths)
            default = console_model_actions.model_sync_history("", {}, _Ctx())
            self.assertEqual(25, default["total"])
            self.assertEqual(20, len(default["history"]))
            self.assertTrue(default["truncated"])
            self.assertEqual(1, default["skipped"])
            self.assertEqual(20, default["limit"])
            full = console_model_actions.model_sync_history(
                "", {"limit": 50}, _Ctx())
            self.assertEqual(25, len(full["history"]))
            self.assertFalse(full["truncated"])
            _assert_unchanged(self, paths, before)

    def test_history_without_file_is_legal_empty(self):
        with temp_sync_env():
            result = console_model_actions.model_sync_history("", {}, _Ctx())
        self.assertEqual([], result["history"])
        self.assertFalse(result["truncated"])

    def test_history_rejects_out_of_range_limit(self):
        for limit in (0, 51, "20", True):
            with self.assertRaises(OperationError) as caught:
                console_model_actions.model_sync_history(
                    "", {"limit": limit}, _Ctx())
            self.assertEqual("invalid_params", caught.exception.code)


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
        # 失败不丢唯一状态：v2 文档迁移只丢 pinned，managed/probes 等原样保留；
        # 迁移只发生在内存副本上，磁盘文件保持原样直到下一次事务写。
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
            "contextOverrides": {},
        }
        with temp_sync_env(state_text=json.dumps(legacy, ensure_ascii=False)) as paths:
            migrated = model_sync.load_state()
            # Rev8 B8-2/B8-3：v4 —— pinned 丢弃 + contextOverrides 迁移为
            # modelSettings 草稿（本夹具无覆盖 → 建空档）。
            self.assertEqual(4, migrated["version"])
            self.assertNotIn("pinned", migrated)
            self.assertNotIn("contextOverrides", migrated)
            self.assertEqual({}, migrated["modelSettings"])
            for key, value in legacy.items():
                if key in ("pinned", "version", "contextOverrides"):
                    continue
                self.assertEqual(value, migrated[key], key)
            self.assertEqual(model_sync_core.STATE_VERSION,
                             migrated["version"])
            self.assertNotIn("pinned", migrated)
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
        # 用模块绝对路径调用 CLI：discover 从仓库根跑，相对路径 "model_sync/sync.py"
        # 会被解析到仓库根（文件打不开），测不到 argparse 的拒绝语义。
        with temp_sync_env():
            proc = subprocess.run(
                [sys.executable, model_sync.__file__, "--pin", PIN_KEY],
                capture_output=True, timeout=60)
        self.assertEqual(2, proc.returncode)
        stderr = proc.stderr.decode("utf-8", "replace")
        self.assertIn("--pin", stderr)
        self.assertIn("unrecognized arguments", stderr)


class TaskActionTests(unittest.TestCase):
    def _run_patched(self, fake, handler):
        with mock.patch("console.model_actions.subprocess.run", fake):
            return handler()

    def test_task_status_is_read_only(self):
        fake = _FakeTaskProcess()
        result = self._run_patched(
            fake,
            lambda: console_model_actions.model_task_status("", {}, _Ctx()))
        self.assertEqual(1, len(fake.commands))
        fake.assert_command(self, fake.commands[0], "Status")
        self.assertFalse(result["installed"])
        self.assertEqual("未安装", result["state"])
        self.assertIsNone(fake.task)

    def test_task_status_failure_maps_to_task_failed(self):
        fake = _FakeTaskProcess()
        fake.fail = True
        with self.assertRaises(OperationError) as caught:
            self._run_patched(
                fake,
                lambda: console_model_actions.model_task_status("", {}, _Ctx()))
        self.assertEqual("task_failed", caught.exception.code)
        self.assertIn("boom", caught.exception.message)

    def test_task_install_is_idempotent(self):
        fake = _FakeTaskProcess()

        def install_twice():
            first = console_model_actions.model_task_install("", {}, _Ctx())
            second = console_model_actions.model_task_install("", {}, _Ctx())
            return first, second

        first, second = self._run_patched(fake, install_twice)
        self.assertEqual(2, len(fake.commands))
        for cmd in fake.commands:
            fake.assert_command(self, cmd, "Repair")
        # Register-ScheduledTask -Force semantics: exactly one task slot,
        # re-registration replaces it, never duplicates.
        self.assertEqual(2, fake.registrations)
        self.assertEqual(1, len(fake.task))
        self.assertTrue(first["installed"])
        self.assertTrue(second["installed"])

    def test_task_install_failure_maps_to_task_failed(self):
        fake = _FakeTaskProcess()
        fake.fail = True
        with self.assertRaises(OperationError) as caught:
            self._run_patched(
                fake,
                lambda: console_model_actions.model_task_install("", {}, _Ctx()))
        self.assertEqual("task_failed", caught.exception.code)
        self.assertIn("计划任务安装/修复失败", caught.exception.message)

    def test_task_disable_forwards_script_result_and_writes_nothing(self):
        fake = _FakeTaskProcess()
        with temp_sync_env() as paths:
            before = _snapshot(paths)
            result = self._run_patched(
                fake,
                lambda: console_model_actions.model_task_disable("", {}, _Ctx()))
            _assert_unchanged(self, paths, before)
        self.assertEqual(1, len(fake.commands))
        fake.assert_command(self, fake.commands[0], "Disable")
        self.assertEqual(1, fake.disables)
        self.assertTrue(result["disabled"])
        self.assertEqual(0, result["exit_code"])
        self.assertTrue(result["output"])
        self.assertNotIn("state", result)

    def test_task_disable_failure_maps_to_task_failed(self):
        fake = _FakeTaskProcess()
        fake.fail = True
        with temp_sync_env() as paths:
            before = _snapshot(paths)
            with self.assertRaises(OperationError) as caught:
                self._run_patched(
                    fake,
                    lambda: console_model_actions.model_task_disable(
                        "", {}, _Ctx()))
            _assert_unchanged(self, paths, before)
        self.assertEqual("task_failed", caught.exception.code)
        self.assertIn("计划任务禁用失败", caught.exception.message)
        self.assertEqual(1, len(fake.commands))
        self.assertEqual("Disable", fake.commands[0][-1])

    def test_task_timeout_maps_to_task_timeout(self):
        def timeout_run(cmd, capture_output=True, timeout=None):
            raise subprocess.TimeoutExpired("powershell", timeout)
        with mock.patch("console.model_actions.subprocess.run",
                        side_effect=timeout_run):
            with self.assertRaises(OperationError) as caught:
                console_model_actions.model_task_install("", {}, _Ctx())
        self.assertEqual("task_timeout", caught.exception.code)


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
    validated against the CURRENT catalog (collected read-only via the
    mocked collector), version conflicts surface as version_conflict, and
    the honest boundary 覆盖保存≠客户端已应用 stays on every result."""

    def _catalogs(self):
        return {"um": [CatalogModel("um", "model-a", context=128000),
                       CatalogModel("um", "model-b", context=None)]}

    def test_set_writes_override_and_reports_apply_boundary(self):
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
            # Rev8 契约 6：保存只写 modelSettings 草稿（draftVersion 递增），
            # 绝不写旧 contextOverrides 键，也绝不触碰客户端配置。
            record = after["modelSettings"]["um|model-a"]
            self.assertEqual(record["draft"]["contextTokens"], 200000)
            self.assertEqual(record["draft"]["contextSource"], "manual")
            datetime_like = record["draftSavedAt"]
            self.assertIsInstance(datetime_like, str)  # ISO stamp
            self.assertEqual(result["source"], "um")
            self.assertEqual(result["context"], 200000)
            self.assertEqual(result["set_at"], record["draftSavedAt"])
            self.assertEqual(result["draft_version"], 1)
            self.assertIn("保存≠已应用", result["note"])
            self.assertIn("草稿", result["note"])
            # Client configs are untouched: this action is state-only.
            self.assertEqual((paths["DSH_YAML"].read_bytes(),
                              paths["ZCODE_JSON"].read_bytes()),
                             (before["DSH_YAML"][0], before["ZCODE_JSON"][0]))

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
                # 冲突是纯拒绝：磁盘文件保持夹具原样字节（迁移只发生在
                # load_state 的内存副本上，写路径才落盘）。
                self.assertEqual(
                    paths["STATE_FILE"].read_text("utf-8"), state_text)
                # 内存迁移结果：v4 草稿保留 200000（draftSavedAt 继承 set_at）。
                migrated = model_sync.load_state()
                self.assertEqual(
                    migrated["modelSettings"]["um|model-a"]
                    ["draft"]["contextTokens"], 200000)
                self.assertNotIn("contextOverrides", migrated)
                # Matching version → update allowed.
                result = console_model_actions.model_context_override_set(
                    "", {"source": "um", "model_id": "model-a",
                         "context": 64000,
                         "expected_set_at": "2026-09-30T08:00:00+00:00"},
                    _Ctx())
                self.assertEqual(result["context"], 64000)
                self.assertEqual(result["draft_version"], 2)

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
            # Rev8 契约 6：恢复也形成可预览草稿（contextSource=None），
            # 绝不静默删除存储；探测历史证据原样保留。
            self.assertIn("恢复草稿已保存", result["note"])
            after = json.loads(paths["STATE_FILE"].read_text("utf-8"))
            record = after["modelSettings"]["um|model-a"]
            self.assertIsNone(record["draft"]["contextSource"])
            self.assertIsNone(record["draft"]["contextTokens"])
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

    def test_read_surface_normalizes_applied_fallback_and_draft_version(self):
        # R5 读面归一化：已应用恒为全四键（缺键补 None），无记录时草稿版本为
        # 0（合法的「没有草稿记录」）——前端据此回退已应用值并携带版本。
        state = model_sync.empty_state()
        state["modelSettings"] = {
            "um|partial": {
                "draft": None, "draftVersion": 1, "draftSavedAt": None,
                "applied": {"alias": "kept"}, "appliedVersion": 1,
                "appliedAt": "t"},
            # 垃圾记录（非字典）不炸只读读面：字段如实为 None。
            "um|junk": {"draft": "junk", "draftVersion": "junk",
                        "applied": "junk", "appliedVersion": True},
        }
        catalogs = {"um": [CatalogModel("um", "partial", context=128000),
                          CatalogModel("um", "fresh", context=128000),
                          CatalogModel("um", "junk", context=128000)]}
        rows = {row["model_id"]: row
                for row in model_sync.context_override_surface(
                    state, catalogs)}
        partial = rows["partial"]
        self.assertEqual(partial["settings_applied"], {
            "alias": "kept", "contextSource": None,
            "contextTokens": None, "defaultEffort": None})
        self.assertEqual(partial["settings_draft_version"], 1)
        self.assertEqual(partial["settings_applied_version"], 1)
        # 无记录的行：草稿版本读作 0（保存时按 expected=0 做并发检查），
        # 已应用与草稿如实为 None。
        fresh = rows["fresh"]
        self.assertEqual(fresh["settings_draft_version"], 0)
        self.assertIsNone(fresh["settings_draft"])
        self.assertIsNone(fresh["settings_applied"])
        junk = rows["junk"]
        self.assertIsNone(junk["settings_draft"])
        self.assertIsNone(junk["settings_applied"])
        self.assertEqual(junk["settings_draft_version"], 0)

    def test_read_surface_reports_declared_override_and_inert(self):
        state = model_sync.empty_state()
        state["modelSettings"] = {
            "um|model-a": {
                "draft": None, "draftVersion": 0, "draftSavedAt": None,
                "applied": {"alias": None, "contextSource": "manual",
                            "contextTokens": 200000,
                            "defaultEffort": None},
                "appliedVersion": 1,
                "appliedAt": "2026-09-30T08:00:00+00:00",
                "appliedEvidence": "applied"},
            # Inert: the model left the catalog — honest unknown, never
            # reported as applied.
            "um|ghost": {
                "draft": None, "draftVersion": 0, "draftSavedAt": None,
                "applied": {"alias": None, "contextSource": "manual",
                            "contextTokens": 64000, "defaultEffort": None},
                "appliedVersion": 1,
                "appliedAt": "2026-09-30T09:00:00+00:00",
                "appliedEvidence": "applied"},
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
        # F1 additive fields: applied 状态与 apply 路径同判定。
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
        # 模型不在当前目录：inert 的唯一合法原因之一（另一是值损坏）。
        self.assertFalse(inert["override_applied"])
        self.assertEqual(inert["override_state"], "model_missing")
        self.assertIn("覆盖保存≠客户端已应用", result["note"])

    def test_read_surface_override_on_none_declaration_is_applied(self):
        # F1: 目录声明 None + 合法人工覆盖 → 与 apply 路径同判定
        # （200000 != None 成立）：effective_source=override、
        # effective_context=覆盖值、override_state=applied——不再互相矛盾
        # 地报 inert/unknown 却在 apply 时真实改写镜像。
        state = model_sync.empty_state()
        state["modelSettings"] = {"um|model-b": {
            "draft": None, "draftVersion": 0, "draftSavedAt": None,
            "applied": {"alias": None, "contextSource": "manual",
                        "contextTokens": 200000, "defaultEffort": None},
            "appliedVersion": 1, "appliedAt": "2026-09-30T10:00:00+00:00",
            "appliedEvidence": "applied"}}
        consumers = {"um|DSH|openwebui": {"models": {"model-b": {
            "contextWindow": 200000}}}}
        with temp_sync_env(state_text=json.dumps(
                state, ensure_ascii=False)) as paths:
            before = _snapshot(paths)
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(self._catalogs(), [])),                     mock.patch.object(model_sync, "collect_consumers",
                                      return_value=(consumers, None, None)):
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
        # Rev8 契约 6：客户端实际读回值单列——读回 == 已应用时
        # client_matches_applied=True；写入成功证据不混称客户端已生效。
        self.assertEqual(row["client_readback_state"], "ok")
        self.assertEqual(row["client_readback_consumer"], "DSH")
        self.assertEqual(row["client_context"], 200000)
        self.assertTrue(row["client_matches_applied"])

    def test_read_surface_equal_override_stays_declared(self):
        # F1: 合法覆盖但等于目录声明——值未改写，来源不夸大：
        # effective_source 保持 declared、effective_context 为声明值、
        # override_state=equal、override_applied=False。
        state = model_sync.empty_state()
        state["modelSettings"] = {"um|model-a": {
            "draft": None, "draftVersion": 0, "draftSavedAt": None,
            "applied": {"alias": None, "contextSource": "manual",
                        "contextTokens": 128000, "defaultEffort": None},
            "appliedVersion": 1, "appliedAt": "2026-09-30T10:00:00+00:00",
            "appliedEvidence": "applied"}}
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
            (console_model_actions.model_sync_preview, {}),
            (console_model_actions.model_sync_apply, {}),
            (console_model_actions.model_sync_pending, {}),
            (console_model_actions.model_sync_history, {}),
            (console_model_actions.model_catalog, {}),
            (console_model_actions.model_task_status, {}),
            (console_model_actions.model_task_install, {}),
            (console_model_actions.model_task_disable, {}),
            (console_model_actions.model_context_probe_preview, {}),
            (console_model_actions.model_context_probe_run, {}),
        ]
        with mock.patch.object(model_sync, "run_sync") as run_sync, \
                mock.patch.object(model_sync, "collect_catalogs") as collect, \
                mock.patch.object(console_model_actions,
                                  "_run_task_script") as task_script:
            for handler, params in cases:
                with self.assertRaises(OperationCancelled):
                    handler("", params, ctx)
            run_sync.assert_not_called()
            collect.assert_not_called()
            task_script.assert_not_called()


class ModelSettingsActionTests(unittest.TestCase):
    """Rev8 契约 5/6 的 console 动作层回归：保存只写草稿、selection 版本
    绑定、幂等零草稿、单事务发布、过期预览拒绝假发布、ZCode 延期→草稿
    回退不标已生效（deferred_restored 分支）。

    夹具沿用 temp_sync_env（隔离 data/home 布局）；collect_catalogs 统一
    mock（identity 采集与 run_sync 共用同一假目录）；zcode_running 显式
    控制以触发/绕过延期分支；ZCode 供应商规则库夹具先对齐（R20-2：
    规则库可用是「写主配置」的前提，延期分支由 zcode_running 单独触发，
    不与 store_problem 混杂）。
    """

    @staticmethod
    def _catalogs():
        return {"um": [CatalogModel("um", "model-a", context=128000)]}

    def test_save_writes_draft_only_and_leaves_clients_untouched(self):
        with temp_sync_env() as paths:
            before = _snapshot(paths)
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(self._catalogs(), [])):
                result = self._save_save({"alias": "别名A"})
            after = json.loads(paths["STATE_FILE"].read_text("utf-8"))
            client_bytes = (paths["DSH_YAML"].read_bytes(),
                            paths["ZCODE_JSON"].read_bytes())
        record = after["modelSettings"]["um|model-a"]
        self.assertEqual(
            record["draft"],
            {"alias": "别名A", "contextSource": None,
             "contextTokens": None, "defaultEffort": None})
        self.assertEqual(record["draftVersion"], 1)
        self.assertIsNone(record["applied"])
        self.assertEqual(result["draft_version"], 1)
        self.assertIn("保存≠已应用", result["note"])
        # 保存绝不触碰客户端配置。
        self.assertEqual(client_bytes,
                         (before["DSH_YAML"][0], before["ZCODE_JSON"][0]))

    def _save_save(self, fields, extra=None):
        params = {"source": "um", "model_id": "model-a"}
        params.update(fields or {})
        params.update(extra or {})
        return console_model_actions.model_settings_save("", params, _Ctx())

    def test_save_rejects_unknown_and_invalid_fields(self):
        with temp_sync_env():
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(self._catalogs(), [])):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_settings_save(
                        "", {"source": "um", "model_id": "model-a",
                             "bogus": 1}, _Ctx())
                self.assertEqual("invalid_params", caught.exception.code)
                self.assertIn("未知设置字段", caught.exception.message)
                for fields in ({"alias": ""}, {"alias": "x" * 129},
                               {"contextSource": "psychic"},
                               {"contextSource": "manual"},
                               {"contextSource": "declared",
                                "contextTokens": 5}):
                    with self.subTest(fields=fields):
                        params = dict(fields, source="um",
                                      model_id="model-a")
                        with self.assertRaises(OperationError) as caught:
                            console_model_actions.model_settings_save(
                                "", params, _Ctx())
                        self.assertEqual("invalid_params",
                                         caught.exception.code)

    def test_save_rejects_effort_outside_catalog(self):
        # 默认档校验针对当前目录 efforts：目录只声明 low/medium 时，"max"
        # 被拒（绝不让未经校验的档位进入草稿）。
        with temp_sync_env():
            with mock.patch.object(
                    model_sync, "collect_catalogs",
                    return_value=({"um": [CatalogModel(
                        "um", "model-a", context=128000,
                        efforts=("low", "medium"))]}, [])):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_settings_save(
                        "", {"source": "um", "model_id": "model-a",
                             "defaultEffort": "max"}, _Ctx())
        self.assertEqual("invalid_params", caught.exception.code)

    def test_save_version_conflict_maps_to_version_conflict(self):
        with temp_sync_env() as paths:
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(self._catalogs(), [])):
                self._save_save({"contextSource": "manual",
                                 "contextTokens": 200000})
            saved = json.loads(paths["STATE_FILE"].read_text("utf-8"))
            version = saved["modelSettings"]["um|model-a"]["draftVersion"]
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(self._catalogs(), [])):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_settings_save(
                        "", {"source": "um", "model_id": "model-a",
                             "contextSource": "manual",
                             "contextTokens": 64000,
                             "expected_draft_version": version + 1}, _Ctx())
            after = json.loads(paths["STATE_FILE"].read_text("utf-8"))
        self.assertEqual("version_conflict", caught.exception.code)
        # 冲突是纯拒绝：磁盘草稿版本与值都不变。
        self.assertEqual(
            after["modelSettings"]["um|model-a"]["draftVersion"], version)
        self.assertEqual(
            after["modelSettings"]["um|model-a"]
            ["draft"]["contextTokens"], 200000)

    def test_settings_apply_rejects_invalid_selection_shape(self):
        with temp_sync_env():
            for selection in ("um|model-a", {"um|model-a": "1"}, {"": 1}):
                with self.subTest(selection=selection):
                    with self.assertRaises(OperationError) as caught:
                        console_model_actions.model_settings_apply(
                            "", {"selection": selection}, _Ctx())
                    self.assertEqual("invalid_params",
                                     caught.exception.code)

    def test_settings_apply_idempotent_when_no_draft(self):
        with temp_sync_env() as paths:
            before = _snapshot(paths, ("DSH_YAML", "ZCODE_JSON"))
            with mock.patch.object(model_sync, "zcode_running",
                                   return_value=False):
                result = console_model_actions.model_settings_apply(
                    "", {"source": "um"}, _Ctx())
            self.assertEqual(result["applied"], [])
            self.assertEqual(result["status"], "normal")
            self.assertIn("幂等", result["note"])
            _assert_unchanged(self, paths, before)

    def test_settings_apply_publishes_via_single_transaction(self):
        # promote → run_sync(state_override=…) 单事务：DSH 侧 contextWindow
        # 按已应用值写入，草稿清空、applied 元数据落盘；deferred_restored 空。
        state = model_sync.empty_state()
        state["modelSettings"] = self._seed_state(1)
        result, after, dsh_text, _ = self._apply(
            state, {"selection": {"um|model-a": 1}})
        self.assertEqual(result["applied"], ["um|model-a"])
        self.assertEqual(result["deferred_restored"], [])
        self.assertEqual(result["status"], "changed")
        record = after["modelSettings"]["um|model-a"]
        self.assertEqual(record["applied"]["contextTokens"], 200000)
        self.assertIsNone(record["draft"])
        self.assertEqual(record["appliedVersion"], 1)
        self.assertEqual(record["appliedEvidence"], "applied")
        # 同一事务里客户端配置真实携带已应用值（DSH contextWindow）。
        self.assertIn("200000", dsh_text)

    @staticmethod
    def _seed_state(version):
        return {"um|model-a": {
            "draft": {"alias": None, "contextSource": "manual",
                      "contextTokens": 200000, "defaultEffort": None},
            "draftVersion": version, "draftSavedAt": "t0",
            "applied": None, "appliedVersion": None, "appliedAt": None,
            "appliedEvidence": None}}

    def test_settings_apply_stale_selection_conflicts_without_publish(self):
        # 过期预览绑定：selection 版本与存储 draftVersion 不一致 →
        # version_conflict，绝不假发布（客户端零写入、磁盘保持夹具原样）。
        state = model_sync.empty_state()
        state["modelSettings"] = self._seed_state(2)
        state_text = json.dumps(state, ensure_ascii=False)
        with temp_sync_env(state_text=state_text) as paths:
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(self._catalogs(), [])):
                with self.assertRaises(OperationError) as caught:
                    console_model_actions.model_settings_apply(
                        "", {"selection": {"um|model-a": 99}}, _Ctx())
            # 过期绑定是纯拒绝：磁盘保持夹具原样字节（绝不假发布）。
            self.assertEqual(paths["STATE_FILE"].read_text("utf-8"),
                             state_text)
        self.assertEqual("version_conflict", caught.exception.code)

    def test_settings_apply_deferral_restores_zcode_drafts(self):
        # ZCode 运行中：整组 ZCode 写入延期——被提升的草稿必须回退为草稿
        # （同版本保留），已应用元数据清除，绝不标已生效；deferred_restored
        # 如实列出回退键；ZCode 主配置未被写（延期）。
        state = model_sync.empty_state()
        state["modelSettings"] = self._seed_state(2)
        result, after, zcode_bytes, before_zcode = self._apply_deferral()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["deferred_restored"], ["um|model-a"])
        record = after["modelSettings"]["um|model-a"]
        self.assertEqual(record["draft"]["contextTokens"], 200000)
        self.assertEqual(record["draftVersion"], 2)
        self.assertIsNone(record["applied"])
        self.assertIsNone(record["appliedVersion"])
        self.assertIsNone(record["appliedEvidence"])
        self.assertEqual(zcode_bytes, before_zcode)

    def _apply_deferral(self):
        state = model_sync.empty_state()
        state["modelSettings"] = self._seed_state(2)
        with temp_sync_env(state_text=json.dumps(state, ensure_ascii=False)) \
                as paths:
            before = _snapshot(paths)
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(_aligned_store(self._catalogs()),
                           ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(self._catalogs(), [])), \
                    mock.patch.object(model_sync, "zcode_running",
                                      return_value=True), \
                    mock.patch.object(model_sync, "zcode_hot_sync_admission",
                                      return_value=(False, "测试：未准入")), \
                    mock.patch.object(model_sync, "_get_json",
                                      side_effect=OSError("offline")):
                params = {"selection": {"um|model-a": 2}}
                preview = console_model_actions.model_settings_preview("", params, _Ctx())
                result = console_model_actions.model_settings_apply(
                    "", dict(params, preview_token=preview["preview_token"]), _Ctx())
            after = json.loads(paths["STATE_FILE"].read_text("utf-8"))
            zcode_bytes = paths["ZCODE_JSON"].read_bytes()
            before_zcode = before["ZCODE_JSON"][0]
        return result, after, zcode_bytes, before_zcode

    def _apply(self, state, params):
        state_text = json.dumps(state, ensure_ascii=False)
        with temp_sync_env(state_text=state_text) as paths:
            paths["ZCODE_PROVIDER_CONFIG"].write_text(
                json.dumps(_aligned_store(self._catalogs()),
                           ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(model_sync, "collect_catalogs",
                                   return_value=(self._catalogs(), [])), \
                    mock.patch.object(model_sync, "zcode_running",
                                      return_value=False), \
                    mock.patch.object(model_sync, "_get_json",
                                      side_effect=OSError("offline")):
                preview = console_model_actions.model_settings_preview("", params, _Ctx())
                result = console_model_actions.model_settings_apply(
                    "", dict(params, preview_token=preview["preview_token"]), _Ctx())
            after = json.loads(paths["STATE_FILE"].read_text("utf-8"))
            dsh_text = paths["DSH_YAML"].read_text("utf-8")
            zcode_bytes = paths["ZCODE_JSON"].read_bytes()
        return result, after, dsh_text, zcode_bytes


if __name__ == "__main__":
    unittest.main()
