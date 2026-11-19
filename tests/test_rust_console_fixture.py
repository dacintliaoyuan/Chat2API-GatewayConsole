# -*- coding: utf-8 -*-
"""Rust S5a console 行为夹具的新鲜度检查（Ticket #51）。

`tests/fixtures/rust_console_rev1.json` 必须由
`tests/generate_rust_console_fixture.py` 从真实 Python 云端
`usagesrv.Handler` 生成。本模块只启动 loopback 临时服务器与临时目录，
不绑定生产端口、不读写生产数据。
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "rust_console_rev1.json"

sys.path.insert(0, str(ROOT / "tests"))


class RustConsoleFixtureTests(unittest.TestCase):
    def test_fixture_is_fresh_from_the_real_python_cloud_console(self):
        self.skipTest("Python closure retired (#90); the source/generator is frozen")
        import generate_rust_console_fixture as generator

        self.assertEqual(
            generator.generate(),
            json.loads(FIXTURE.read_text(encoding="utf-8")),
            "Rust console fixture is stale; run "
            "python tests/generate_rust_console_fixture.py",
        )

    def test_fixture_records_every_acceptance_surface(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertTrue(fixture["metadata"]["fixture"])
        self.assertEqual(fixture["summary"]["role"], "cloud")
        self.assertFalse(fixture["summary"]["hasSources"])
        self.assertEqual(fixture["services"]["service"]["state"], "Stopped")
        self.assertEqual(fixture["consoleHealth"]["host"]["id"], "usagesrv")
        self.assertTrue(fixture["actions"]["body"]["actions"])
        self.assertIn("empty", fixture["operations"])
        self.assertTrue(fixture["operations"]["seeded"]["rows"])
        self.assertEqual(len(fixture["capability"]), 9)
        for name in (
            "duplicateHeader",
            "duplicateContentType",
            "invalidContentLength",
            "tooLarge",
            "transferEncoding",
            "contentEncoding",
            "missingContentType",
            "wrongContentType",
            "invalidJson",
            "nonObject",
            "nullBody",
            "unknownFields",
            "emptyPost",
            "foreignHost",
            "foreignOrigin",
            "alias",
        ):
            self.assertIn(name, fixture["boundary"])


if __name__ == "__main__":
    unittest.main()
