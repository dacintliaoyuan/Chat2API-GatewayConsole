# -*- coding: utf-8 -*-
"""最终退役静态审计（Ticket #35）。

``scripts/check_retired_features.py`` 把“退役功能不再出现在生产入口/引用/
文档/夹具里”变成可机械验收的检查。本模块验证审计脚本本身：

* 当前工作树通过审计，且脚本已接入 ``scripts/check.sh``；
* allowlist 里的路径逐条存在、是显式文件而非通配符；
* allowlist 之外的退役词命中会失败，allowlist 之内（断言不存在的测试）放行；
* ``node_modules`` / ``web/dist*`` / ``__pycache__`` 不参与扫描；
* deny 清单覆盖各退役面（术语、动作、端口、模块）。
"""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_retired_features.py"
CHECK_SH = ROOT / "scripts" / "check.sh"


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "check_retired_features_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RetiredFeatureAuditTests(unittest.TestCase):
    def test_script_exists_and_current_tree_is_clean(self):
        self.assertTrue(SCRIPT.is_file(),
                        "scripts/check_retired_features.py 不存在")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(
            0, proc.returncode,
            "审计失败:\n%s\n%s" % (proc.stdout, proc.stderr))
        self.assertIn("PASS", proc.stdout)

    def test_allowlist_entries_are_explicit_existing_files(self):
        audit = _load_audit()
        self.assertTrue(audit.ALLOWLIST)
        for relative in audit.ALLOWLIST:
            self.assertNotIn("*", relative)
            self.assertTrue((ROOT / relative).is_file(), relative)
        self.assertIn("scripts/check_retired_features.py", audit.ALLOWLIST)

    def test_retired_term_outside_allowlist_is_a_violation(self):
        audit = _load_audit()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chat2api").mkdir()
            (root / "chat2api" / "production.py").write_text(
                "RETIRED = 'workbench'\n", encoding="utf-8")
            violations = audit.find_violations(root, allowlist=())
        self.assertEqual(1, len(violations))
        self.assertIn("workbench", violations[0])
        self.assertIn("chat2api/production.py", violations[0])

    def test_unreadable_file_fails_closed_with_a_reason(self):
        audit = _load_audit()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chat2api").mkdir()
            path = root / "chat2api" / "bad.py"
            path.write_text("clean\n", encoding="utf-8")
            original = Path.read_text

            def read_text(self, *args, **kwargs):
                if self == path:
                    raise OSError("permission denied")
                return original(self, *args, **kwargs)

            with mock.patch.object(Path, "read_text", read_text):
                violations = audit.find_violations(root, allowlist=())
        self.assertEqual(1, len(violations))
        self.assertIn("chat2api/bad.py", violations[0])
        self.assertIn("unreadable", violations[0])
        self.assertIn("permission denied", violations[0])

    def test_non_utf8_file_fails_closed_with_a_reason(self):
        audit = _load_audit()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chat2api").mkdir()
            (root / "chat2api" / "bad.py").write_bytes(b"workbench \xff\n")
            violations = audit.find_violations(root, allowlist=())
        self.assertEqual(1, len(violations))
        self.assertIn("chat2api/bad.py", violations[0])
        self.assertIn("unreadable", violations[0])
        self.assertIn("UnicodeDecodeError", violations[0])

    def test_retired_term_inside_allowlist_is_allowed(self):
        audit = _load_audit()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests" / "test_absence.py").write_text(
                "assert 'workbench' not in names\n", encoding="utf-8")
            violations = audit.find_violations(
                root, allowlist=("tests/test_absence.py",))
        self.assertEqual([], violations)

    def test_ignored_directories_are_skipped(self):
        audit = _load_audit()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("node_modules/x.py", "web/dist/app.js",
                             "web/dist-cloud/app.js", "__pycache__/x.py"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("workbench\n", encoding="utf-8")
            violations = audit.find_violations(root, allowlist=())
        self.assertEqual([], violations)

    def test_unregistered_assertion_test_is_reported(self):
        audit = _load_audit()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests" / "test_new_absence.py").write_text(
                "assert 'workbench' not in names\n", encoding="utf-8")
            violations = audit.find_violations(root, allowlist=())
        self.assertEqual(1, len(violations))
        self.assertIn("allowlist", violations[0].lower())
        self.assertIn("tests/test_new_absence.py", violations[0])

    def test_deny_categories_cover_retired_surfaces(self):
        audit = _load_audit()
        names = {rule[0] for rule in audit.DENY_RULES}
        for required in (
                "workbench", "speedtest", "qoder action", "cliproxy action",
                "m365 action", "qoderQuota", "zcode", "8110",
                "share.backend_", "share.chain_", "model.sync_",
                "model.settings_", "model.task_", "pricing_rules",
                "magpie_prices", "price env", "public_supervision",
                "share_dispatch", "backend_supervision",
                "consumer helpers", "retired services gate",
                "dsh-cost-meter", "retired cost fields",
                "retired usage wording", "retired share actions",
                "cloud dispatch", "old share entry", "share_admin bat",
                "retired source ledger env"):
            self.assertIn(required, names)

    def test_check_sh_invokes_the_audit(self):
        text = CHECK_SH.read_text(encoding="utf-8")
        self.assertIn("scripts/check_retired_features.py", text)


if __name__ == "__main__":
    unittest.main()
