# -*- coding: utf-8 -*-
"""`ai-gateway/tools/zcode-config/keepset_apply.py` 的夹具测试（不碰真实配置）。

覆盖：

* 保留集比对的四态（SAME / ADDED / REMOVED / OUT_OF_SCOPE）与阻塞项
  （规则块缺席的 provider：产品渲染函数只对齐已存在的块，工具不新建块）；
* `--apply` / `--revert` 的写入门：ZCode 在运行即拒绝、备份哈希不符即拒绝、
  写后读回不符即拒绝；
* 保留集陈旧（状态文件受管登记不一致）与 `code_stamp` 不一致的不可判定分支；
* 结构差异里疑似凭据字段打码。

隔离：夹具全在 `tempfile` 临时目录里，`--config` / `--keepset` / `--state` /
`--backup-root` 一律注入；**不读也不写真实 `~/.zcode/v2/provider_config.json`**、
不读 `ai-gateway/data/`、**不查本机进程**（`model_config_adapters.zcode_running`
被换成抛错替身，任何未注入 `running` 的调用都会失败）、不启动/停止 ZCode、
不发模型请求、不生成密钥。

运行（工作目录 `ai-gateway/`）：

    python -B -m unittest tests.test_zcode_config_tools -q
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

AI_GATEWAY_DIR = Path(__file__).resolve().parents[1]
TOOL_PATH = AI_GATEWAY_DIR / "tools" / "zcode-config" / "keepset_apply.py"

# 先按产品模块的路径把 ai-gateway 加进 sys.path（工具模块自己也会加一次）。
if str(AI_GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(AI_GATEWAY_DIR))

import model_config_adapters  # noqa: E402


def _load_tool():
    spec = importlib.util.spec_from_file_location("zcode_config_keepset_apply",
                                                 TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()

PRODUCT_CODE_STAMP = tool.model_sync_module().CODE_STAMP

UM_BINDING = "um|ZCode|local:openwebui"
CN_BINDING = "qoder_cn|ZCode|local:lingma-proxy"
CLIPROXY_BINDING = "cliproxy|ZCode|local:cliproxy"

SENTINEL_API_KEY = "SENTINEL-API-KEY-DO-NOT-PRINT"


# --------------------------------------------------------------- 夹具构造
def _store(entries):
    """规则库夹具：``entries`` 每项 ``{provider, ids, rules}``。"""
    provider_rules, model_rules = [], []
    for entry in entries:
        provider = entry["provider"]
        ids = list(entry.get("ids") or [])
        provider_rules.append({
            "providerId": provider,
            "providerName": entry.get("provider_name") or provider,
            "enabled": True,
            "config": {"access": "custom", "api": "openai", "group": "local",
                       "modelOrder": list(ids), "personalModelIds": list(ids)},
        })
        for model_id in entry.get("rules") or []:
            model_rules.append({
                "modelId": model_id, "providerId": provider,
                "config": {"properties": {"contextWindow": 4096}},
            })
    return {"schemaVersion": 1, "config": {
        "providerOrder": [entry["provider"] for entry in entries],
        "providerConfigRules": {"providerRules": provider_rules},
        "modelConfigRules": {"providerModelRules": model_rules},
    }}


def _keepset(entries, code_stamp=None):
    """保留集夹具：``entries`` 每项 ``(binding, keep, retired)``。"""
    zcode = {}
    for binding, keep, retired in entries:
        zcode[binding] = {"keep": list(keep), "retired": list(retired),
                         "managed": list(keep) + list(retired)}
    return {"derived_at": "2026-09-27T10:44:24+08:00",
            "code_stamp": PRODUCT_CODE_STAMP if code_stamp is None
            else code_stamp,
            "zcode": zcode}


def _state(keepset):
    """状态文件夹具：受管登记与保留集的 ``managed`` 一致（默认不陈旧）。"""
    managed = {}
    for key, entry in keepset["zcode"].items():
        managed[key] = {model_id: {"retired": False}
                        for model_id in entry["managed"]}
    return {"version": 2, "managed": managed}


def _write_json(path, payload):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2)
                          + "\n", encoding="utf-8")


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ToolCase(unittest.TestCase):
    """临时目录夹具 + 「绝不查本机进程」替身。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = self.root / "provider_config.json"
        self.keepset_path = self.root / "zcode-keepset.json"
        self.state_path = self.root / "model-sync-state.json"
        self.backup_root = self.root / "output"
        original = model_config_adapters.zcode_running

        def forbidden():
            raise AssertionError("测试不得查本机 ZCode 进程")

        model_config_adapters.zcode_running = forbidden
        self.addCleanup(setattr, model_config_adapters, "zcode_running", original)

    def run_tool(self, argv, running):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tool.main(argv, running=running)
        return code, out.getvalue()

    def check(self, running=lambda: False):
        return self.run_tool(["--check", "--config", str(self.config),
                             "--keepset", str(self.keepset_path),
                             "--state", str(self.state_path)], running)

    def apply(self, running=lambda: False, stamp=None):
        argv = ["--apply", "--config", str(self.config),
                "--keepset", str(self.keepset_path),
                "--state", str(self.state_path),
                "--backup-root", str(self.backup_root)]
        if stamp:
            argv += ["--stamp", stamp]
        return self.run_tool(argv, running)

    def revert(self, backup, running=lambda: False):
        return self.run_tool(["--revert", "--backup", str(backup),
                             "--config", str(self.config)], running)

    def write_fixture(self, store, keepset, state=None):
        _write_json(self.config, store)
        _write_json(self.keepset_path, keepset)
        _write_json(self.state_path, state if state is not None
                     else _state(keepset))


# --------------------------------------------------------------- 四态与差异
class FourStatesTest(ToolCase):
    def fixture(self):
        # B0 (2026-11-01): UM is the only active ZCode binding. The retired
        # qoder_cn / cliproxy bindings no longer exist in the product, so a
        # keep-set naming them is refused as unknown instead of being projected.
        store = _store([
            {"provider": "local:openwebui",
             "ids": ["DeepSeek-V4.1-Flash", "model-a", "cn-a", "manual-x"],
             "rules": ["DeepSeek-V4.1-Flash", "model-a", "cn-a"]},
        ])
        keepset = _keepset([
            (UM_BINDING, ["DeepSeek-V4.1-Flash", "GLM-OCR", "added-b",
                          "added-c"], ["model-a", "cn-a"]),
        ])
        return store, keepset

    def test_four_states_and_counts(self):
        store, keepset = self.fixture()
        self.write_fixture(store, keepset)
        before = self.config.read_bytes()
        code, text = self.check()
        self.assertEqual(code, 2)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.backup_root.exists())
        self.assertIn("[SAME] DeepSeek-V4.1-Flash", text)
        self.assertIn("[ADDED] GLM-OCR", text)
        self.assertIn("[REMOVED] model-a", text)
        self.assertIn("[REMOVED] cn-a", text)
        self.assertIn("[OUT_OF_SCOPE] manual-x", text)
        self.assertNotIn("DROPPED", text)
        self.assertIn("统计：SAME=1 ADDED=3 REMOVED=2 OUT_OF_SCOPE=1", text)
        self.assertIn("结论：与保留集不一致（退出码 2）", text)

    def test_manual_entry_stays_in_place(self):
        store, keepset = self.fixture()
        self.write_fixture(store, keepset)
        result = tool.check_keepset(self.config, self.keepset_path,
                                   self.state_path, running=lambda: False)
        entries = tool.keepset_entries(keepset)
        bindings, unknown = tool.bindings_for(entries, tool.product_bindings())
        self.assertEqual(unknown, [])
        projected = json.loads(tool.project_text(
            tool.read_text(self.config), entries, bindings))
        ids = tool.rule_store_surface(projected)["local:openwebui"]
        # 手工条目（不在任何受管登记里）逐字节不动，且保持原位置。
        self.assertEqual(ids["personalModelIds"],
                         ["DeepSeek-V4.1-Flash", "manual-x", "GLM-OCR",
                          "added-b", "added-c"])
        # 手工条目本来就没有每模型规则条目，渲染函数也不会给它新建；
        # model-a / cn-a 的规则条目按退役离开。
        self.assertEqual(ids["rules"],
                         ["DeepSeek-V4.1-Flash", "GLM-OCR", "added-b",
                          "added-c"])
        statuses = {model["model_id"]: model["status"]
                    for model in result["classified"]["blocks"][0]["models"]}
        self.assertEqual(statuses, {"DeepSeek-V4.1-Flash": "SAME",
                                   "GLM-OCR": "ADDED", "added-b": "ADDED",
                                   "added-c": "ADDED", "model-a": "REMOVED",
                                   "cn-a": "REMOVED",
                                   "manual-x": "OUT_OF_SCOPE"})

    def test_missing_rule_block_is_blocked(self):
        # The only active ZCode binding has no provider block in this store:
        # every keep id stays blocked instead of the tool inventing a block.
        store = _store([
            {"provider": "local:lingma-proxy",
             "ids": ["cn-a"], "rules": ["cn-a"]},
        ])
        keepset = _keepset([
            (UM_BINDING, ["DeepSeek-V4.1-Flash", "GLM-OCR"], ["model-a"]),
        ])
        self.write_fixture(store, keepset)
        code, text = self.check()
        self.assertEqual(code, 2)
        self.assertIn("[ADDED·BLOCKED] DeepSeek-V4.1-Flash", text)
        self.assertIn("[ADDED·BLOCKED] GLM-OCR", text)
        self.assertIn("阻塞：", text)
        self.assertIn("local:openwebui：2 条 keep 无法补齐", text)
        # A blocked keep-set is a decidable failure: --check must not claim
        # the JSON layer is equal with exit code 0 just because no block exists.
        self.assertIn("结论：与保留集不一致（退出码 2）", text)

    def test_apply_refuses_while_rule_block_missing(self):
        store = _store([
            {"provider": "local:lingma-proxy",
             "ids": ["cn-a"], "rules": ["cn-a"]},
        ])
        keepset = _keepset([
            (UM_BINDING, ["DeepSeek-V4.1-Flash", "GLM-OCR"], ["model-a"]),
        ])
        self.write_fixture(store, keepset)
        before = self.config.read_bytes()
        code, text = self.apply()
        self.assertEqual(code, 2)
        self.assertIn("rule_block_missing:local:openwebui", text)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.backup_root.exists())

    def test_aligned_store_is_exit_zero(self):
        store = _store([
            {"provider": "local:openwebui", "ids": ["DeepSeek-V4.1-Flash"],
             "rules": ["DeepSeek-V4.1-Flash"]},
        ])
        keepset = _keepset([(UM_BINDING, ["DeepSeek-V4.1-Flash"], [])])
        self.write_fixture(store, keepset)
        raw = tool.read_text(self.config)
        entries = tool.keepset_entries(keepset)
        bindings, _ = tool.bindings_for(entries, tool.product_bindings())
        projected = tool.project_text(raw, entries, bindings)
        self.config.write_text(projected, encoding="utf-8", newline="")
        # 投影幂等：再投影一次不产生新差异。
        self.assertEqual(tool.project_text(projected, entries, bindings),
                         projected)
        before = self.config.read_bytes()
        code, text = self.check()
        self.assertEqual(code, 0)
        self.assertIn("结论：投影与当前规则库在 JSON 层逐键相等（退出码 0）", text)
        self.assertIn("统计：SAME=1 ADDED=0 REMOVED=0 OUT_OF_SCOPE=0", text)
        self.assertIn("JSON 差异 0 条", text)
        self.assertEqual(self.config.read_bytes(), before)

    def test_apply_reports_no_difference_without_writing(self):
        store = _store([
            {"provider": "local:openwebui", "ids": ["DeepSeek-V4.1-Flash"],
             "rules": ["DeepSeek-V4.1-Flash"]},
        ])
        keepset = _keepset([(UM_BINDING, ["DeepSeek-V4.1-Flash"], [])])
        self.write_fixture(store, keepset)
        raw = tool.read_text(self.config)
        entries = tool.keepset_entries(keepset)
        bindings, _ = tool.bindings_for(entries, tool.product_bindings())
        self.config.write_text(tool.project_text(raw, entries, bindings),
                                encoding="utf-8", newline="")
        before = self.config.read_bytes()
        code, text = self.apply()
        self.assertEqual(code, 0)
        self.assertIn("no_difference", text)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.backup_root.exists())


# ------------------------------------------------------- 写入门与备份 / 回退
class ApplyRevertTest(ToolCase):
    def fixture(self):
        store = _store([
            {"provider": "local:openwebui",
             "ids": ["DeepSeek-V4.1-Flash", "model-a"],
             "rules": ["DeepSeek-V4.1-Flash", "model-a"]},
        ])
        keepset = _keepset([(UM_BINDING, ["DeepSeek-V4.1-Flash"], ["model-a"])])
        return store, keepset

    def test_apply_backs_up_writes_and_reads_back(self):
        store, keepset = self.fixture()
        self.write_fixture(store, keepset)
        original = self.config.read_bytes()
        code, text = self.apply(stamp="20260927-120000")
        self.assertEqual(code, 0)
        self.assertIn("原因码：applied（退出码 0）", text)
        written = self.config.read_bytes()
        self.assertNotEqual(written, original)
        doc = json.loads(written.decode("utf-8"))
        surface = tool.rule_store_surface(doc)["local:openwebui"]
        self.assertEqual(surface["personalModelIds"], ["DeepSeek-V4.1-Flash"])
        self.assertEqual(surface["modelOrder"], ["DeepSeek-V4.1-Flash"])
        self.assertEqual(surface["rules"], ["DeepSeek-V4.1-Flash"])
        backup_dir = self.backup_root / "20260927-120000"
        self.assertEqual((backup_dir / "provider_config.json.bak").read_bytes(),
                         original)
        self.assertEqual((backup_dir / "provider_config.json.sha256")
                         .read_text(encoding="ascii").strip(),
                         hashlib.sha256(original).hexdigest())
        self.assertIn("写后读回 sha256=%s"
                     % hashlib.sha256(written).hexdigest(), text)

    def test_revert_restores_and_checks_hash(self):
        store, keepset = self.fixture()
        self.write_fixture(store, keepset)
        original = self.config.read_bytes()
        self.assertEqual(self.apply(stamp="20260927-120001")[0], 0)
        backup_dir = self.backup_root / "20260927-120001"
        code, text = self.revert(backup_dir)
        self.assertEqual(code, 0)
        self.assertIn("原因码：reverted（退出码 0）", text)
        self.assertEqual(self.config.read_bytes(), original)
        # 也可以直接给 .bak 文件路径。
        self.assertEqual(self.apply(stamp="20260927-120002")[0], 0)
        code, _ = self.revert(backup_dir / "provider_config.json.bak")
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_bytes(), original)

    def test_backup_dir_is_not_overwritten(self):
        payload = b'{"a": 1}\n'
        info = tool.write_backup(self.backup_root, payload, stamp="fixed")
        self.assertEqual(info["digest"], hashlib.sha256(payload).hexdigest())
        with self.assertRaises(OSError):
            tool.write_backup(self.backup_root, payload, stamp="fixed")
        # 既有备份不被覆盖：第二次调用没有改写第一次的内容。
        self.assertEqual(Path(info["backup_path"]).read_bytes(), payload)

    def test_apply_refuses_while_zcode_running(self):
        store, keepset = self.fixture()
        self.write_fixture(store, keepset)
        before = self.config.read_bytes()
        code, text = self.apply(running=lambda: True)
        self.assertEqual(code, 2)
        self.assertIn("原因码：zcode_running", text)
        self.assertIn("未写盘", text)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.backup_root.exists())

    def test_revert_refuses_while_zcode_running(self):
        store, keepset = self.fixture()
        self.write_fixture(store, keepset)
        self.assertEqual(self.apply(stamp="20260927-120004")[0], 0)
        applied = self.config.read_bytes()
        backup_dir = self.backup_root / "20260927-120004"
        code, text = self.revert(backup_dir, running=lambda: True)
        self.assertEqual(code, 2)
        self.assertIn("原因码：zcode_running", text)
        self.assertEqual(self.config.read_bytes(), applied)

    def test_revert_refuses_when_digest_mismatch(self):
        store, keepset = self.fixture()
        self.write_fixture(store, keepset)
        original = self.config.read_bytes()
        self.assertEqual(self.apply(stamp="20260927-120005")[0], 0)
        applied = self.config.read_bytes()
        backup_dir = self.backup_root / "20260927-120005"
        (backup_dir / "provider_config.json.sha256").write_text(
            "0" * 64 + "\n", encoding="ascii", newline="\n")
        code, text = self.revert(backup_dir)
        self.assertEqual(code, 2)
        self.assertIn("原因码：backup_digest_mismatch", text)
        self.assertEqual(self.config.read_bytes(), applied)
        self.assertNotEqual(original, applied)

    def test_revert_refuses_when_digest_missing(self):
        store, keepset = self.fixture()
        self.write_fixture(store, keepset)
        self.assertEqual(self.apply(stamp="20260927-120006")[0], 0)
        applied = self.config.read_bytes()
        backup_dir = self.backup_root / "20260927-120006"
        (backup_dir / "provider_config.json.sha256").unlink()
        code, text = self.revert(backup_dir)
        self.assertEqual(code, 2)
        self.assertIn("原因码：backup_digest_missing", text)
        self.assertEqual(self.config.read_bytes(), applied)

    def test_revert_refuses_when_backup_missing_or_not_json(self):
        self.write_fixture(*self.fixture())
        code, text = self.revert(self.backup_root / "nope")
        self.assertEqual(code, 2)
        self.assertIn("原因码：backup_missing", text)
        broken = self.backup_root / "broken"
        broken.mkdir(parents=True)
        payload = b"not json"
        (broken / "provider_config.json.bak").write_bytes(payload)
        (broken / "provider_config.json.sha256").write_text(
            hashlib.sha256(payload).hexdigest() + "\n", encoding="ascii",
            newline="\n")
        code, text = self.revert(broken)
        self.assertEqual(code, 2)
        self.assertIn("原因码：backup_not_json", text)

    def test_apply_refuses_when_backup_fails(self):
        store, keepset = self.fixture()
        self.write_fixture(store, keepset)
        before = self.config.read_bytes()
        original = tool.write_backup
        self.addCleanup(setattr, tool, "write_backup", original)

        def broken(*_args, **_kwargs):
            raise OSError("backup_readback_mismatch")

        tool.write_backup = broken
        code, text = self.apply()
        self.assertEqual(code, 2)
        self.assertIn("原因码：backup_failed:OSError", text)
        self.assertEqual(self.config.read_bytes(), before)

    def test_apply_reports_readback_mismatch(self):
        store, keepset = self.fixture()
        self.write_fixture(store, keepset)
        original = tool.write_bytes
        self.addCleanup(setattr, tool, "write_bytes", original)

        def broken(path, _data):
            original(path, b"{}")

        tool.write_bytes = broken
        code, text = self.apply(stamp="20260927-120007")
        self.assertEqual(code, 2)
        self.assertIn("原因码：readback_mismatch", text)

    def test_revert_reports_readback_mismatch(self):
        store, keepset = self.fixture()
        self.write_fixture(store, keepset)
        self.assertEqual(self.apply(stamp="20260927-120008")[0], 0)
        original = tool.write_bytes
        self.addCleanup(setattr, tool, "write_bytes", original)

        def broken(path, _data):
            original(path, b"{}")

        tool.write_bytes = broken
        code, text = self.revert(self.backup_root / "20260927-120008")
        self.assertEqual(code, 2)
        self.assertIn("原因码：readback_mismatch", text)

    def test_revert_requires_backup_argument(self):
        store, keepset = self.fixture()
        self.write_fixture(store, keepset)
        with self.assertRaises(SystemExit) as revert_error:
            with contextlib.redirect_stderr(io.StringIO()):
                tool.main(["--revert", "--config", str(self.config)],
                          running=lambda: False)
        self.assertEqual(revert_error.exception.code, 2)
        with self.assertRaises(SystemExit) as backup_error:
            with contextlib.redirect_stderr(io.StringIO()):
                tool.main(["--check", "--backup", "x"],
                          running=lambda: False)
        self.assertEqual(backup_error.exception.code, 2)


# ------------------------------------------------------------ 不可判定与自检
class UndecidableInputTest(ToolCase):
    def test_stale_keepset_is_refused(self):
        store = _store([{"provider": "local:openwebui",
                         "ids": ["DeepSeek-V4.1-Flash"],
                         "rules": ["DeepSeek-V4.1-Flash"]}])
        keepset = _keepset([(UM_BINDING, ["DeepSeek-V4.1-Flash"], [])])
        state = _state(keepset)
        state["managed"][UM_BINDING]["GLM-OCR"] = {"retired": False}
        self.write_fixture(store, keepset, state)
        before = self.config.read_bytes()
        code, text = self.check()
        self.assertEqual(code, 2)
        self.assertIn("原因码：keepset_stale", text)
        self.assertIn("保留集已陈旧", text)
        self.assertEqual(self.config.read_bytes(), before)

    def test_missing_state_binding_is_stale(self):
        store = _store([{"provider": "local:openwebui",
                         "ids": ["DeepSeek-V4.1-Flash"],
                         "rules": ["DeepSeek-V4.1-Flash"]}])
        keepset = _keepset([(UM_BINDING, ["DeepSeek-V4.1-Flash"], [])])
        self.write_fixture(store, keepset, state={"managed": {}})
        code, text = self.check()
        self.assertEqual(code, 2)
        self.assertIn("原因码：keepset_stale", text)

    def test_code_stamp_mismatch_is_refused(self):
        store = _store([{"provider": "local:openwebui",
                         "ids": ["DeepSeek-V4.1-Flash"],
                         "rules": ["DeepSeek-V4.1-Flash"]}])
        keepset = _keepset([(UM_BINDING, ["DeepSeek-V4.1-Flash"], [])],
                           code_stamp="deadbeef0000")
        self.write_fixture(store, keepset)
        before = self.config.read_bytes()
        code, text = self.check()
        self.assertEqual(code, 2)
        self.assertIn("原因码：code_stamp_mismatch:deadbeef0000", text)
        self.assertEqual(self.config.read_bytes(), before)
        # --apply 同样按不可判定拒绝，且不写盘。
        code, text = self.apply()
        self.assertEqual(code, 2)
        self.assertIn("code_stamp_mismatch", text)
        self.assertEqual(self.config.read_bytes(), before)

    def test_unreadable_inputs_are_not_success(self):
        keepset = _keepset([(UM_BINDING, ["DeepSeek-V4.1-Flash"], [])])
        _write_json(self.keepset_path, keepset)
        _write_json(self.state_path, _state(keepset))
        code, text = self.check()
        self.assertEqual(code, 2)
        self.assertIn("原因码：config_unreadable", text)
        self.config.write_text("{ not json", encoding="utf-8")
        code, text = self.check()
        self.assertEqual(code, 2)
        self.assertIn("原因码：config_not_json", text)
        self.config.unlink()
        self.keepset_path.unlink()
        code, text = self.check()
        self.assertEqual(code, 2)
        self.assertIn("原因码：keepset_unreadable", text)

    def test_unknown_binding_is_refused(self):
        store = _store([{"provider": "local:openwebui",
                         "ids": ["DeepSeek-V4.1-Flash"],
                         "rules": ["DeepSeek-V4.1-Flash"]}])
        keepset = _keepset([("nope|ZCode|local:nope", ["x"], [])])
        self.write_fixture(store, keepset)
        code, text = self.check()
        self.assertEqual(code, 2)
        self.assertIn("原因码：keepset_binding_unknown:nope|ZCode|local:nope",
                      text)

    def test_keepset_structure_is_checked(self):
        self.assertIsNone(tool.validate_keepset(
            _keepset([(UM_BINDING, ["a"], [])])))
        broken = _keepset([(UM_BINDING, ["a"], [])])
        broken["zcode"][UM_BINDING]["managed"] = ["a", "b"]
        self.assertEqual(tool.validate_keepset(broken),
                         "keepset_managed_mismatch:" + UM_BINDING)
        overlap = _keepset([(UM_BINDING, ["a"], ["a"])])
        self.assertEqual(tool.validate_keepset(overlap),
                         "keepset_keep_retired_overlap:" + UM_BINDING)
        wrong_consumer = _keepset([("um|DSH|openwebui", ["a"], [])])
        self.assertEqual(tool.validate_keepset(wrong_consumer),
                         "keepset_binding_invalid:um|DSH|openwebui")
        self.assertEqual(tool.validate_keepset({}), "keepset_missing_zcode")

    def test_duplicate_source_is_refused(self):
        store = _store([{"provider": "local:openwebui",
                         "ids": ["DeepSeek-V4.1-Flash"],
                         "rules": ["DeepSeek-V4.1-Flash"]}])
        keepset = _keepset([(UM_BINDING, ["DeepSeek-V4.1-Flash"], []),
                            ("um|ZCode|local:openwebui-2", ["GLM-OCR"], [])])
        self.write_fixture(store, keepset)
        code, text = self.check()
        self.assertEqual(code, 2)
        self.assertIn("原因码：keepset_binding_unknown", text)


# -------------------------------------------------------------- 输出与默认值
class OutputAndDefaultsTest(ToolCase):
    def test_json_diff_redacts_credentials(self):
        before = {"config": {"api": {"apiKey": SENTINEL_API_KEY,
                                    "baseURL": "http://127.0.0.1:8320/v1"}}}
        after = {"config": {"api": {"apiKey": "other-value",
                                   "baseURL": "http://127.0.0.1:8320/v2"}}}
        diff = tool.json_diff(before, after)
        self.assertEqual(len(diff), 2)
        for item in diff:
            self.assertIn(item["before"], (tool.REDACTED,
                                          "http://127.0.0.1:8320/v1"))
            self.assertIn(item["after"], (tool.REDACTED,
                                          "http://127.0.0.1:8320/v2"))
        self.assertNotIn(SENTINEL_API_KEY, json.dumps(diff, ensure_ascii=False))

    def test_text_layer_note_reports_missing_trailing_newline(self):
        canonical = '{"a": 1}\n'
        self.assertTrue(tool.text_layer_note(canonical, canonical)["byte_equal"])
        note = tool.text_layer_note('{"a": 1}', canonical)
        self.assertFalse(note["byte_equal"])
        self.assertIn("缺尾换行", note["detail"])
        self.assertTrue(tool.text_layer_note('\ufeff{"a": 1}\n',
                                            canonical)["bom"])

    def test_check_prints_credentials_never(self):
        store = _store([{"provider": "local:openwebui",
                         "ids": ["DeepSeek-V4.1-Flash"],
                         "rules": ["DeepSeek-V4.1-Flash"]}])
        store["config"]["providerConfigRules"]["providerRules"][0]["config"][
            "api"] = {"apiKey": SENTINEL_API_KEY}
        # 规则库里多出一个受管 id：保证 --check 有差异、必须打印差异与统计。
        keepset = _keepset([(UM_BINDING, ["DeepSeek-V4.1-Flash", "GLM-OCR"],
                                 [])])
        self.write_fixture(store, keepset)
        code, text = self.check()
        self.assertEqual(code, 2)
        self.assertIn("统计：SAME=1 ADDED=1", text)
        self.assertNotIn(SENTINEL_API_KEY, text)

    def test_zcode_running_delegates_to_product(self):
        model_config_adapters.zcode_running = lambda: True
        self.assertTrue(tool.zcode_running())

    def test_default_paths_and_backup_root(self):
        self.assertEqual(tool.DEFAULT_CONFIG,
                         Path.home() / ".zcode" / "v2" / "provider_config.json")
        self.assertEqual(tool.DEFAULT_BACKUP_ROOT,
                         tool.REPO_ROOT / "output")
        self.assertEqual(tool.DEFAULT_KEEPSET.parent.parent.name, "output")
        self.assertEqual(tool.DEFAULT_KEEPSET.name, "zcode-keepset.json")
        ignored = (tool.REPO_ROOT / ".gitignore").read_text(
            encoding="utf-8").splitlines()
        self.assertIn("/output/", [line.strip() for line in ignored])
        # 默认状态文件只在 ai-gateway/data/ 里读，工具不写它。
        self.assertEqual(tool.DEFAULT_STATE,
                         AI_GATEWAY_DIR / "data" / "model-sync-state.json")

    def test_backup_bytes_are_read_back(self):
        payload = b'{"a": 1}\n'
        info = tool.write_backup(self.backup_root, payload, stamp="fixed")
        self.assertEqual(Path(info["backup_path"]).read_bytes(), payload)
        self.assertEqual(info["digest"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(Path(info["digest_path"]).read_text(
            encoding="ascii").strip(), info["digest"])
        self.assertEqual(info["bytes"], len(payload))

    def test_keepset_entries_sorted_and_validated(self):
        keepset = _keepset([(UM_BINDING, ["b", "a"], []),
                             (CN_BINDING, ["Auto"], ["cn-a"])])
        entries = tool.keepset_entries(keepset)
        self.assertEqual([entry["key"] for entry in entries],
                         [CN_BINDING, UM_BINDING])
        self.assertEqual(entries[0]["managed"], ["Auto", "cn-a"])
        self.assertEqual(entries[0]["retired"], ["cn-a"])


if __name__ == "__main__":
    unittest.main()
