# -*- coding: utf-8 -*-
"""model_reconcile 纯规则测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_sync.reconcile import (
    KNOWN_MODELS, UM_SUFFIX, RepairItem, ProblemItem,
    ReconcileSnapshot, ReconcileResult, is_variant_of, reconcile,
)


def _make_dsh_config(m365_models=None, um_models=None):
    """构造一个最小 DSH settings.yaml 解析后的 dict。"""
    return {
        "llm-pi-ai": {
            "providers": {
                "m365": {"models": m365_models or []},
                "openwebui": {"models": um_models or []},
            }
        }
    }


def _make_zcode_config(models=None):
    """构造一个最小 ZCode config.json 解析后的 dict。"""
    return {
        "provider": {
            "local:openwebui": {
                "models": models or {}
            }
        }
    }


class TestKnownModels(unittest.TestCase):
    def test_known_models_have_efforts_and_default(self):
        for mid, info in KNOWN_MODELS.items():
            self.assertIn("efforts", info)
            self.assertIn("default", info)
            self.assertIsInstance(info["efforts"], list)
            self.assertTrue(info["default"] in info["efforts"])

    def test_glm_has_xhigh(self):
        self.assertIn("xhigh", KNOWN_MODELS["GLM-5.2-NVFP4"]["efforts"])


class TestIsVariantOf(unittest.TestCase):
    def test_fast_variant_detected(self):
        self.assertTrue(is_variant_of("DeepSeek-V4-Flash-0731-Fast",
                                       {"DeepSeek-V4-Flash-0731"}))

    def test_non_variant(self):
        self.assertFalse(is_variant_of("DeepSeek-V4-Flash-0731",
                                        {"DeepSeek-V4-Flash-0731"}))

    def test_variant_base_not_in_set(self):
        self.assertFalse(is_variant_of("Foo-Fast", {"Bar"}))


class TestReconcileDSH(unittest.TestCase):
    def test_dsh_missing_m365_provider(self):
        config = {"llm-pi-ai": {"providers": {"openwebui": {"models": []}}}}
        snap = ReconcileSnapshot(dsh_config=config)
        result = reconcile(snap)
        self.assertTrue(any("缺少 m365" in p.text for p in result.problems))

    def test_dsh_missing_openwebui_provider(self):
        config = {"llm-pi-ai": {"providers": {"m365": {"models": []}}}}
        snap = ReconcileSnapshot(dsh_config=config)
        result = reconcile(snap)
        self.assertTrue(any("缺少 openwebui" in p.text for p in result.problems))

    def test_dsh_um_ctx_mismatch_produces_fix(self):
        upstream = {"GLM-5.2-NVFP4": 128000}
        um_models = [{"id": "GLM-5.2-NVFP4", "contextWindow": 64000}]
        config = _make_dsh_config(um_models=um_models)
        snap = ReconcileSnapshot(
            upstream=upstream, gw_models=["GLM-5.2-NVFP4"],
            gw_up=True, dsh_config=config)
        result = reconcile(snap)
        ctx_fixes = [f for f in result.fixes
                     if f.target == "DSH" and f.kind == "ctx"
                     and f.mid == "GLM-5.2-NVFP4"]
        self.assertEqual(len(ctx_fixes), 1)
        self.assertEqual(ctx_fixes[0].ctx, 128000)

    def test_dsh_m365_um_artifact_when_m365_available(self):
        upstream = {"GLM-5.2-NVFP4": 128000}
        m365_models = []  # GLM not in M365
        m365_provider_models = [{"id": "GLM-5.2-NVFP4", "name": "GLM（UM）"}]
        config = _make_dsh_config(m365_models=m365_provider_models)
        snap = ReconcileSnapshot(
            upstream=upstream, m365_models=m365_models,
            m365_available=True, dsh_config=config)
        result = reconcile(snap)
        remove_fixes = [f for f in result.fixes
                        if f.target == "DSH" and f.kind == "remove"
                        and f.mid == "GLM-5.2-NVFP4"]
        self.assertEqual(len(remove_fixes), 1)

    def test_dsh_m365_um_artifact_not_removed_when_m365_unavailable(self):
        """4141 清单不可用时不判定移除残留。"""
        upstream = {"GLM-5.2-NVFP4": 128000}
        m365_provider_models = [{"id": "GLM-5.2-NVFP4", "name": "GLM（UM）"}]
        config = _make_dsh_config(m365_models=m365_provider_models)
        snap = ReconcileSnapshot(
            upstream=upstream, m365_available=False, dsh_config=config)
        result = reconcile(snap)
        remove_fixes = [f for f in result.fixes
                        if f.target == "DSH" and f.kind == "remove"]
        self.assertEqual(len(remove_fixes), 0)


class TestReconcileZCode(unittest.TestCase):
    def test_zcode_ctx_mismatch_produces_fix(self):
        upstream = {"GLM-5.2-NVFP4": 128000}
        models = {
            "GLM-5.2-NVFP4": {
                "limit": {"context": 64000, "output": 128000},
                "name": "GLM-5.2-NVFP4（UM）",
            }
        }
        config = _make_zcode_config(models)
        snap = ReconcileSnapshot(
            upstream=upstream, zcode_config=config)
        result = reconcile(snap)
        ctx_fixes = [f for f in result.fixes
                     if f.target == "ZCode" and f.kind == "ctx"]
        self.assertEqual(len(ctx_fixes), 1)
        self.assertEqual(ctx_fixes[0].ctx, 128000)

    def test_zcode_missing_um_suffix_produces_name_fix(self):
        upstream = {"GLM-5.2-NVFP4": 128000}
        models = {
            "GLM-5.2-NVFP4": {
                "limit": {"context": 128000, "output": 128000},
                "name": "GLM-5.2-NVFP4",
            }
        }
        config = _make_zcode_config(models)
        snap = ReconcileSnapshot(
            upstream=upstream, zcode_config=config)
        result = reconcile(snap)
        name_fixes = [f for f in result.fixes
                      if f.target == "ZCode" and f.kind == "name"]
        self.assertEqual(len(name_fixes), 1)

    def test_zcode_model_missing_from_snapshot_is_warning_only(self):
        upstream = {"OtherModel": 128000}  # OldModel not in upstream = offline
        models = {
            "OldModel": {
                "limit": {"context": 128000, "output": 128000},
                "name": "OldModel（UM）",
            }
        }
        config = _make_zcode_config(models)
        snap = ReconcileSnapshot(
            upstream=upstream, zcode_config=config)
        result = reconcile(snap)
        remove_fixes = [f for f in result.fixes
                        if f.target == "ZCode" and f.kind == "remove"
                        and f.mid == "OldModel"]
        self.assertEqual(remove_fixes, [])
        self.assertTrue(any("仅警告" in p.text for p in result.problems))

    def test_zcode_new_upstream_model_produces_add_fix(self):
        upstream = {"NewModel": 200000}
        config = _make_zcode_config(models={})
        snap = ReconcileSnapshot(
            upstream=upstream, zcode_config=config)
        result = reconcile(snap)
        add_fixes = [f for f in result.fixes
                     if f.target == "ZCode" and f.kind == "add"
                     and f.mid == "NewModel"]
        self.assertEqual(len(add_fixes), 1)
        self.assertEqual(add_fixes[0].ctx, 200000)
        self.assertIsNone(add_fixes[0].efforts)  # not in KNOWN_MODELS

    def test_zcode_new_known_model_gets_efforts(self):
        upstream = {"GLM-5.2-NVFP4": 128000}
        config = _make_zcode_config(models={})
        snap = ReconcileSnapshot(
            upstream=upstream, zcode_config=config)
        result = reconcile(snap)
        add_fixes = [f for f in result.fixes
                     if f.target == "ZCode" and f.kind == "add"
                     and f.mid == "GLM-5.2-NVFP4"]
        self.assertEqual(len(add_fixes), 1)
        self.assertEqual(add_fixes[0].efforts,
                         KNOWN_MODELS["GLM-5.2-NVFP4"]["efforts"])

    def test_zcode_effort_model_fast_variant(self):
        upstream = {"DeepSeek-V4-Flash-0731": 128000}
        config = _make_zcode_config(models={})
        snap = ReconcileSnapshot(
            upstream=upstream, zcode_config=config,
            effort_model_bases={"DeepSeek-V4-Flash-0731"})
        result = reconcile(snap)
        fast_fixes = [f for f in result.fixes
                      if f.target == "ZCode" and f.kind == "add"
                      and f.mid == "DeepSeek-V4-Flash-0731-Fast"]
        self.assertEqual(len(fast_fixes), 1)
        self.assertIsNone(fast_fixes[0].efforts)

    def test_zcode_unknown_model_no_effort_guess(self):
        """未知模型不自动猜测思考档位。"""
        upstream = {"UnknownModel": 64000}
        config = _make_zcode_config(models={})
        snap = ReconcileSnapshot(
            upstream=upstream, zcode_config=config)
        result = reconcile(snap)
        add_fixes = [f for f in result.fixes
                     if f.target == "ZCode" and f.kind == "add"
                     and f.mid == "UnknownModel"]
        self.assertEqual(len(add_fixes), 1)
        self.assertIsNone(add_fixes[0].efforts)


class TestReconcileVariants(unittest.TestCase):
    def test_variant_base_offline_detected(self):
        upstream = {"OtherModel": 128000}  # base not in upstream
        snap = ReconcileSnapshot(
            upstream=upstream,
            gw_models=["OtherModel", "DeepSeek-V4-Flash-0731-Fast"],
            gw_up=True)
        result = reconcile(snap)
        self.assertTrue(any("变体" in p.text for p in result.problems))


class TestReconcileM365Residual(unittest.TestCase):
    def test_m365_um_overlap_detected(self):
        """M365 中残留 UM 模型但不匹配 m365 清单时产生 remove fix。"""
        upstream = {"GLM-5.2-NVFP4": 128000}
        m365_provider_models = [{"id": "GLM-5.2-NVFP4"}]  # in upstream = UM artifact
        config = _make_dsh_config(m365_models=m365_provider_models)
        snap = ReconcileSnapshot(
            upstream=upstream, m365_models=[],  # GLM not in M365 gateway
            m365_available=True, dsh_config=config)
        result = reconcile(snap)
        self.assertTrue(any("UM 合并残留" in p.text for p in result.problems))


if __name__ == "__main__":
    unittest.main()
