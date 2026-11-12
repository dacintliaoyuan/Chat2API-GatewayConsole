# -*- coding: utf-8 -*-
"""model_config_adapters 配置适配器测试。

使用临时文件验证 DSH YAML 和 ZCode JSON 的读取、应用、备份和重新解析。
不触碰真实用户配置文件。
"""
import errno
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import model_config_adapters

from model_reconcile import RepairItem
from model_sync_core import Binding, CatalogModel, SyncChange
from model_config_adapters import (
    ZCODE_MANAGED_PROVIDER_DEFAULTS,
    dsh_apply_ctx, dsh_apply_remove, dsh_provider_span,
    dsh_read, dsh_apply_fixes,
    zcode_managed_provider_template,
    zcode_models, zcode_new_entry, zcode_read, zcode_apply_fixes,
    render_dsh_sync, render_zcode_sync, apply_config_transaction,
)


_DSH_SAMPLE = """\
version: 1
llm-pi-ai:
  providers:
    m365:
      models:
        - id: M365-Native
          contextWindow: 128000
        - id: GLM-5.2-NVFP4
          contextWindow: 64000
    openwebui:
      models:
        - id: DeepSeek-V4-Flash-0731
          contextWindow: 64000
"""

# 复刻真实 settings.yaml：block 风格 provider 之后紧跟手工维护的 flow 风格
# 章节（其内部的 "    models:" 恰好是 4 空格缩进，曾触发 provider 边界误判）。
_DSH_SAMPLE_WITH_FLOW_TAIL = """\
version: 1
llm-pi-ai:
  providers:
    m365:
      models:
        - id: M365-Native
          contextWindow: 128000
    openwebui:
      models:
        - id: DeepSeek-V4-Flash-0731
          contextWindow: 64000
llm-deepseek:
  {
    models:
      [
        {
            id: deepseek-v4-flash-vision-exp,
            contextWindow: 1000000
          }
      ]
  }
"""

_ZCODE_SAMPLE = {
    "provider": {
        "local:openwebui": {
            "models": {
                "GLM-5.2-NVFP4": {
                    "limit": {"context": 64000, "output": 128000},
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "zcode": {"modified": True, "priority": 50},
                    "name": "GLM-5.2-NVFP4",
                }
            }
        }
    }
}


class TestDshYamlAdapter(unittest.TestCase):
    def test_dsh_apply_ctx(self):
        text = dsh_apply_ctx(_DSH_SAMPLE, "openwebui",
                             "DeepSeek-V4-Flash-0731", 128000)
        self.assertIn("contextWindow: 128000", text)
        # other model unchanged
        self.assertIn("contextWindow: 64000", text)

    def test_dsh_apply_remove(self):
        text = dsh_apply_remove(_DSH_SAMPLE, "m365", "GLM-5.2-NVFP4")
        self.assertNotIn("GLM-5.2-NVFP4", text)
        self.assertIn("M365-Native", text)

    def test_dsh_provider_span(self):
        start, end = dsh_provider_span(_DSH_SAMPLE, "m365")
        block = _DSH_SAMPLE[start:end]
        self.assertIn("M365-Native", block)
        self.assertNotIn("DeepSeek", block)

    def test_dsh_provider_span_last_provider_stops_before_flow_tail(self):
        start, end = dsh_provider_span(_DSH_SAMPLE_WITH_FLOW_TAIL, "openwebui")
        block = _DSH_SAMPLE_WITH_FLOW_TAIL[start:end]
        self.assertIn("DeepSeek-V4-Flash-0731", block)
        self.assertNotIn("llm-deepseek", block)
        self.assertNotIn("deepseek-v4-flash-vision-exp", block)

    def test_dsh_remove_last_provider_with_flow_tail(self):
        text = dsh_apply_remove(
            _DSH_SAMPLE_WITH_FLOW_TAIL, "openwebui", "DeepSeek-V4-Flash-0731")
        self.assertNotIn("DeepSeek-V4-Flash-0731", text)
        parsed = yaml.safe_load(text)
        providers = parsed["llm-pi-ai"]["providers"]
        self.assertEqual(providers["m365"]["models"][0]["id"], "M365-Native")
        self.assertIn("deepseek-v4-flash-vision-exp",
                      parsed["llm-deepseek"]["models"][0]["id"])

    def test_dsh_apply_fixes_with_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.yaml"
            path.write_text(_DSH_SAMPLE, encoding="utf-8", newline="")
            fixes = [RepairItem(
                target="DSH", kind="ctx", mid="DeepSeek-V4-Flash-0731",
                prov="openwebui", ctx=200000,
                desc="test ctx fix")]
            ok = dsh_apply_fixes(path, fixes)
            self.assertTrue(ok)
            # backup exists
            self.assertTrue((path.with_suffix(".yaml.bak")).exists())
            # value updated
            content = path.read_text(encoding="utf-8")
            self.assertIn("contextWindow: 200000", content)

    def test_dsh_read_returns_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.yaml"
            path.write_text(_DSH_SAMPLE, encoding="utf-8", newline="")
            result = dsh_read(path)
            self.assertIsNotNone(result)
            self.assertIn("llm-pi-ai", result)

    def test_dsh_read_missing_file(self):
        result = dsh_read(Path("/nonexistent/settings.yaml"))
        self.assertIsNone(result)


class TestZCodeJsonAdapter(unittest.TestCase):
    def test_zcode_models_accessor(self):
        ms = zcode_models(_ZCODE_SAMPLE)
        self.assertIn("GLM-5.2-NVFP4", ms)

    def test_zcode_new_entry_with_efforts(self):
        entry = zcode_new_entry("GLM-5.2-NVFP4", 128000, 49,
                                ["low", "medium", "high"])
        self.assertEqual(entry["limit"]["context"], 128000)
        self.assertEqual(entry["name"], "GLM-5.2-NVFP4（UM）")
        self.assertTrue(entry["reasoning"]["enabled"])
        self.assertEqual(entry["reasoning"]["defaultVariant"], "medium")

    def test_zcode_new_entry_without_efforts(self):
        entry = zcode_new_entry("UnknownModel", 64000, 49, None)
        self.assertNotIn("reasoning", entry)
        self.assertEqual(entry["name"], "UnknownModel（UM）")

    def test_zcode_apply_fixes_ctx(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(_ZCODE_SAMPLE, ensure_ascii=False, indent=2),
                encoding="utf-8")
            fixes = [RepairItem(
                target="ZCode", kind="ctx", mid="GLM-5.2-NVFP4",
                ctx=200000, desc="test ctx fix")]
            ok = zcode_apply_fixes(path, fixes)
            self.assertTrue(ok)
            self.assertTrue((path.with_suffix(".json.bak")).exists())
            c = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                c["provider"]["local:openwebui"]["models"]
                ["GLM-5.2-NVFP4"]["limit"]["context"], 200000)

    def test_zcode_apply_fixes_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(_ZCODE_SAMPLE, ensure_ascii=False, indent=2),
                encoding="utf-8")
            fixes = [RepairItem(
                target="ZCode", kind="name", mid="GLM-5.2-NVFP4",
                desc="test name fix")]
            ok = zcode_apply_fixes(path, fixes)
            self.assertTrue(ok)
            c = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("（UM）",
                          c["provider"]["local:openwebui"]["models"]
                          ["GLM-5.2-NVFP4"]["name"])

    def test_zcode_apply_fixes_add(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(_ZCODE_SAMPLE, ensure_ascii=False, indent=2),
                encoding="utf-8")
            fixes = [RepairItem(
                target="ZCode", kind="add", mid="NewModel", ctx=64000,
                efforts=None, desc="test add")]
            ok = zcode_apply_fixes(path, fixes)
            self.assertTrue(ok)
            c = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("NewModel",
                          c["provider"]["local:openwebui"]["models"])

    def test_zcode_apply_fixes_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(_ZCODE_SAMPLE, ensure_ascii=False, indent=2),
                encoding="utf-8")
            fixes = [RepairItem(
                target="ZCode", kind="remove", mid="GLM-5.2-NVFP4",
                desc="test remove")]
            ok = zcode_apply_fixes(path, fixes)
            self.assertTrue(ok)
            c = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("GLM-5.2-NVFP4",
                             c["provider"]["local:openwebui"]["models"])

    def test_zcode_read_returns_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(_ZCODE_SAMPLE, ensure_ascii=False),
                encoding="utf-8")
            result = zcode_read(path)
            self.assertIsNotNone(result)
            self.assertIn("provider", result)

    def test_zcode_replace_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            original = json.dumps(_ZCODE_SAMPLE, ensure_ascii=False, indent=2)
            path.write_text(original, encoding="utf-8")
            fixes = [RepairItem(
                target="ZCode", kind="ctx", mid="GLM-5.2-NVFP4",
                ctx=200000, desc="test ctx fix")]
            with mock.patch("model_config_adapters.os.replace",
                            side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    zcode_apply_fixes(path, fixes)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_zcode_read_missing_file(self):
        result = zcode_read(Path("/nonexistent/config.json"))
        self.assertIsNone(result)


class TestCatalogSyncRendering(unittest.TestCase):
    def test_dsh_preserve_context_keeps_the_client_context_window(self):
        # R6/D4：更新项携带 preserve_context 时，客户端手工上下文绝不被
        # 覆盖——渲染器原样保留客户端条目里的 contextWindow。
        binding = Binding("um", "DSH", "openwebui", "UM", "（UM）")
        change = SyncChange(
            binding, "update", "DeepSeek-V4-Flash-0731", "update",
            CatalogModel("um", "DeepSeek-V4-Flash-0731", context=128000,
                          efforts=("low", "high")),
            {"id": "DeepSeek-V4-Flash-0731", "contextWindow": 65536},
            preserve_context=True)
        output = render_dsh_sync(_DSH_SAMPLE, [change])
        parsed = yaml.safe_load(output)
        models = {item["id"]: item for item in
                  parsed["llm-pi-ai"]["providers"]["openwebui"]["models"]}
        self.assertEqual(models["DeepSeek-V4-Flash-0731"]["contextWindow"], 65536)

    def test_zcode_preserve_context_keeps_the_client_context_window(self):
        # 旧版 config.json 迁移路径同样遵守 preserve_context。
        binding = Binding("um", "ZCode", "local:openwebui", "UM", "（UM）")
        change = SyncChange(
            binding, "update", "GLM-5.2-NVFP4", "update",
            CatalogModel("um", "GLM-5.2-NVFP4", context=128000),
            {"name": "GLM-5.2-NVFP4",
             "limit": {"context": 65536, "output": 128000}},
            preserve_context=True)
        output = render_zcode_sync(
            json.dumps(_ZCODE_SAMPLE, ensure_ascii=False), [change])
        model = json.loads(output)["provider"]["local:openwebui"][
            "models"]["GLM-5.2-NVFP4"]
        self.assertEqual(model["limit"]["context"], 65536)

    def test_dsh_add_update_name_and_remove_preserve_other_provider(self):
        binding = Binding("um", "DSH", "openwebui", "UM", "（UM）")
        changes = [
            SyncChange(binding, "provider_name", None, "rename"),
            SyncChange(
                binding, "update", "DeepSeek-V4-Flash-0731", "update",
                CatalogModel(
                    "um", "DeepSeek-V4-Flash-0731", context=128000,
                    efforts=("low", "high"), default_effort="high"),
                {"id": "DeepSeek-V4-Flash-0731", "custom": "kept"}),
            SyncChange(
                binding, "add", "New", "add",
                CatalogModel("um", "New", context=64000)),
        ]
        output = render_dsh_sync(_DSH_SAMPLE, changes)
        parsed = __import__("yaml").safe_load(output)
        providers = parsed["llm-pi-ai"]["providers"]
        self.assertEqual(providers["openwebui"]["displayName"], "UM")
        self.assertEqual(len(providers["m365"]["models"]), 2)
        models = {item["id"]: item for item in providers["openwebui"]["models"]}
        self.assertEqual(models["DeepSeek-V4-Flash-0731"]["custom"], "kept")
        self.assertEqual(models["DeepSeek-V4-Flash-0731"]["contextWindow"], 128000)
        self.assertIn("New", models)

    def test_zcode_updates_selected_provider_only(self):
        sample = json.loads(json.dumps(_ZCODE_SAMPLE))
        sample["provider"]["local:m365"] = {
            "name": "Old M365", "models": {"Old": {"name": "Old"}}}
        binding = Binding("m365", "ZCode", "local:m365", "M365", "（M365）")
        changes = [
            SyncChange(binding, "provider_name", None, "rename"),
            SyncChange(
                binding, "add", "gpt-5.6-sol", "add",
                CatalogModel(
                    "m365", "gpt-5.6-sol", context=128000,
                    efforts=("minimal", "low", "medium", "high", "xhigh"),
                    default_effort="high")),
        ]
        output = render_zcode_sync(
            json.dumps(sample, ensure_ascii=False), changes)
        parsed = json.loads(output)
        self.assertEqual(parsed["provider"]["local:m365"]["name"], "M365")
        model = parsed["provider"]["local:m365"]["models"]["gpt-5.6-sol"]
        self.assertEqual(model["name"], "gpt-5.6-sol（M365）")
        self.assertEqual(model["reasoning"]["defaultVariant"], "high")
        self.assertIn("GLM-5.2-NVFP4", zcode_models(parsed))

    def test_dsh_add_valid_when_flow_tail_follows_last_provider(self):
        binding = Binding("um", "DSH", "openwebui", "UM", "（UM）")
        changes = [
            SyncChange(
                binding, "add", "GLM-5.3-Flash", "add",
                CatalogModel(
                    "um", "GLM-5.3-Flash", context=262144,
                    efforts=("low", "medium", "high"))),
        ]
        output = render_dsh_sync(_DSH_SAMPLE_WITH_FLOW_TAIL, changes)
        parsed = yaml.safe_load(output)
        models = {item["id"]: item for item in
                  parsed["llm-pi-ai"]["providers"]["openwebui"]["models"]}
        self.assertIn("GLM-5.3-Flash", models)
        self.assertEqual(models["GLM-5.3-Flash"]["contextWindow"], 262144)
        self.assertEqual(models["GLM-5.3-Flash"]["name"], "GLM-5.3-Flash（UM）")
        # 尾部 flow 风格章节保持原样可解析
        self.assertEqual(parsed["llm-deepseek"]["models"][0]["id"],
                         "deepseek-v4-flash-vision-exp")

    def test_transaction_rolls_back_first_file_when_second_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text("old-first", encoding="utf-8")
            second.write_text("old-second", encoding="utf-8")
            real_replace = __import__("os").replace
            calls = 0

            def fail_second(src, dst):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("second replace failed")
                return real_replace(src, dst)

            with mock.patch("model_config_adapters.os.replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    apply_config_transaction(
                        {first: "new-first", second: "new-second"},
                        root / "backups", timestamp="20260904-060000")
            self.assertEqual(first.read_text(encoding="utf-8"), "old-first")
            self.assertEqual(second.read_text(encoding="utf-8"), "old-second")


class TestRetiredProviderTemplates(unittest.TestCase):
    """B0（2026-11-01）：退役来源的受管 provider 模板与 Key 读取器已删除。"""

    def test_retired_provider_templates_are_gone(self):
        for provider in ("local:qoder-global", "local:cliproxy"):
            with self.subTest(provider=provider):
                self.assertIsNone(
                    zcode_managed_provider_template(provider))
        self.assertEqual(ZCODE_MANAGED_PROVIDER_DEFAULTS, {})
        self.assertFalse(
            hasattr(model_config_adapters, "CLIPROXY_API_KEY_FILE"))
        self.assertFalse(
            hasattr(model_config_adapters, "CLIPROXY_API_KEY_PLACEHOLDER"))
        self.assertFalse(
            hasattr(model_config_adapters, "cliproxy_api_key"))


class TestConfigPublishTransaction(unittest.TestCase):
    """发布事务：rename 有界重试、版本校验回退与结构化失败（Rev11）。

    每个用例只在自己的临时目录内构造文件；不触碰任何真实客户端配置。
    """

    def _write(self, root, name, text):
        path = Path(root) / name
        path.write_text(text, encoding="utf-8")
        return path

    @staticmethod
    def _busy_error():
        error = PermissionError("busy")
        error.winerror = 5
        return error

    def test_rename_retry_is_bounded_and_then_succeeds(self):
        # 前两次 os.replace 报 Windows 占用类错误（winerror 5），第三次成功：
        # 按退避序列有限重试后落盘，绝不无限重试。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._write(root, "first.json", "old")
            real_replace = os.replace
            calls = {"replace": 0}
            sleeps = []

            def flaky(src, dst):
                calls["replace"] += 1
                if calls["replace"] <= 2:
                    raise self._busy_error()
                return real_replace(src, dst)

            with mock.patch("model_config_adapters.os.replace",
                            side_effect=flaky), \
                    mock.patch("model_config_adapters.time.sleep",
                               side_effect=sleeps.append):
                replaced = apply_config_transaction(
                    {first: "new"}, root / "backups",
                    timestamp="20261004-060000")
            self.assertEqual(replaced, [first])
            self.assertEqual(first.read_text("utf-8"), "new")
            self.assertEqual(calls["replace"], 3)
            self.assertEqual(
                sleeps, list(model_config_adapters.RENAME_RETRY_DELAYS_S[:2]))

    def test_rename_retry_exhausts_and_fails_explicitly(self):
        # 占用错误一直存在：重试到上限后原样抛出结构化失败，文件保持原样；
        # 绝不静默成功，也绝不无限重试。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._write(root, "first.json", "old")
            sleeps = []

            def always_busy(src, dst):
                raise self._busy_error()

            with mock.patch("model_config_adapters.os.replace",
                            side_effect=always_busy), \
                    mock.patch("model_config_adapters.time.sleep",
                               side_effect=sleeps.append):
                with self.assertRaises(
                        model_config_adapters.ConfigPublishError) as caught:
                    apply_config_transaction(
                        {first: "new"}, root / "backups",
                        timestamp="20261004-060000")
            self.assertEqual(first.read_text("utf-8"), "old")
            # 重试次数 = 退避序列长度（首次 + 每次退避一次），耗尽即失败。
            self.assertEqual(
                len(sleeps), len(model_config_adapters.RENAME_RETRY_DELAYS_S))
            self.assertEqual(caught.exception.failed_path, first)
            self.assertEqual(caught.exception.published, [])
            self.assertEqual(caught.exception.rolled_back, [])
            self.assertEqual(caught.exception.left_in_place, [])
            self.assertIn("first.json", str(caught.exception))
            # 明确失败仍可按 OSError 家族捕获（既有调用方兼容）。
            self.assertIsInstance(caught.exception, OSError)

    def test_non_retryable_rename_error_fails_immediately(self):
        # 非占用类错误（ENOENT）立即失败：零退避、零重试。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._write(root, "first.json", "old")
            sleeps = []

            def missing(src, dst):
                raise OSError(errno.ENOENT, "gone")

            with mock.patch("model_config_adapters.os.replace",
                            side_effect=missing), \
                    mock.patch("model_config_adapters.time.sleep",
                               side_effect=sleeps.append):
                with self.assertRaises(
                        model_config_adapters.ConfigPublishError):
                    apply_config_transaction(
                        {first: "new"}, root / "backups",
                        timestamp="20261004-060000")
            self.assertEqual(sleeps, [])
            self.assertEqual(first.read_text("utf-8"), "old")

    def test_rollback_verifies_the_current_version(self):
        # 第二个文件替换失败；第一个文件在失败前已被并发写者改写：
        # 回退必须校验当前版本——绝不把旧快照盖到并发写者的新内容上。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._write(root, "first.json", "old-first")
            second = self._write(root, "second.json", "old-second")
            real_replace = os.replace
            calls = {"replace": 0}

            def fail_second(src, dst):
                calls["replace"] += 1
                if calls["replace"] == 2:
                    # 并发写者：第一个文件替换后、第二个替换失败前改写它。
                    first.write_text("concurrent-edit", encoding="utf-8")
                    raise OSError("second replace failed")
                return real_replace(src, dst)

            with mock.patch("model_config_adapters.os.replace",
                            side_effect=fail_second):
                with self.assertRaises(
                        model_config_adapters.ConfigPublishError) as caught:
                    apply_config_transaction(
                        {first: "new-first", second: "new-second"},
                        root / "backups", timestamp="20261004-060000")
            self.assertEqual(first.read_text("utf-8"), "concurrent-edit")
            self.assertEqual(second.read_text("utf-8"), "old-second")
            self.assertEqual(caught.exception.failed_path, second)
            self.assertEqual(caught.exception.published, [first])
            self.assertEqual(caught.exception.rolled_back, [])
            self.assertEqual(caught.exception.left_in_place, [first])
            self.assertIn("保留现状待核对", str(caught.exception))

    def test_state_write_failure_rolls_back_the_published_client_file(self):
        # 发布成功而状态落盘失败：客户端文件按版本校验回退（仍是本次写入才
        # 回退），失败结果区分已发布/已回退/保留现状；状态文件保持旧字节。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = self._write(root, "provider_config.json", "old-client")
            state = self._write(root, "state.json", "old-state")
            real_replace = os.replace

            def fail_state(src, dst):
                if Path(dst) == state:
                    raise OSError(errno.EACCES, "state busy")
                return real_replace(src, dst)

            with mock.patch("model_config_adapters.os.replace",
                            side_effect=fail_state), \
                    mock.patch("model_config_adapters.time.sleep"):
                with self.assertRaises(
                        model_config_adapters.ConfigPublishError) as caught:
                    apply_config_transaction(
                        {client: "new-client", state: "new-state"},
                        root / "backups", timestamp="20261004-060000")
            self.assertEqual(caught.exception.published, [client])
            self.assertEqual(caught.exception.rolled_back, [client])
            self.assertEqual(caught.exception.left_in_place, [])
            self.assertEqual(caught.exception.failed_path, state)
            self.assertEqual(client.read_text("utf-8"), "old-client")
            self.assertEqual(state.read_text("utf-8"), "old-state")
            message = str(caught.exception)
            self.assertIn("已按版本校验回退", message)
            self.assertIn("不是跨文件事务", message)


if __name__ == "__main__":
    unittest.main()
